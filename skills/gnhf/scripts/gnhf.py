#!/usr/bin/env -S uv run --script

# /// script
# requires-python = ">=3.13,<3.14"
# dependencies = [
#     "python-decouple>=3.8",
#     "requests>=2.32",
# ]
# [tool.uv]
# exclude-newer = "2026-10-01T00:00:00Z"
# ///

# pyright: reportMissingImports=false

"""
Usage:
    gnhf.py -s <pi|opencode> [smoke-test options]
    gnhf.py -l --agent pi -C <cwd> -o <log> [launch options] -- <agent command...>
    gnhf.py -l --agent opencode -C <cwd> -o <log> [launch options]
    gnhf.py --poll <state-file>

Args:
    -s, --smoke-test AGENT   verify AGENT can reach its currently configured
                             model and produce a real response
    -l, --launch             start an unattended run, bounded by --ttl.
                             --agent pi shells out to AGENT COMMAND (after
                             --), matching upstream's subprocess+timeout
                             design. --agent opencode instead starts
                             `opencode serve`, creates a session with the
                             gnhf permission ruleset, and sends the prompt
                             via the server's REST API -- see SKILL.md
                             step 5.
    --poll STATE_FILE        one non-blocking check of an opencode launch
                             started by -l --agent opencode: scans for a
                             MANUAL_RUN marker, rejects any pending
                             permission request outside the gnhf ruleset,
                             aborts the session if the TTL has elapsed.
                             Prints RUNNING / DONE / BAILED / TTL_EXPIRED /
                             SERVER_DIED. Not used for --agent pi launches
                             (those are tailed/ps-checked directly, per
                             SKILL.md step 6 -- pi's flow is otherwise
                             unchanged.)

Note:
    Exit codes are shared across smoke-test and launch modes:
      0  OK             -- PASS: / LAUNCHED:
      1  FAIL           -- unreachable, wrong reply, agent missing
      2  usage error
      3  RATE_LIMITED   -- 429s persisted past the retry ceiling
      4  EARLY_EXIT     -- died inside the probe window, not a 429

    Codes 3 and 4 both mean the task never got a turn. Neither is ever a task
    verdict (BAILED/DONE) -- see SKILL.md step 5.

    Defaults for every tunable below are resolved through python-decouple:
    CLI flag > process env > skills/gnhf/.env > hardcoded default. See
    .env.example for the full list of GNHF_* names.
"""

import argparse
import contextlib
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from decouple import Config, RepositoryEmpty, RepositoryEnv
from pathlib import Path

import requests

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_RATE_LIMITED = 3
EXIT_EARLY_EXIT = 4

SMOKE_TEST_PROMPT = "Reply with exactly this one word and nothing else: PONG"

RATE_LIMIT_PATTERNS = [
    re.compile(r'"code"\s*:\s*"concurrency_limit"'),
    re.compile(r"rate_limit_error", re.IGNORECASE),
    re.compile(r"\b(?:status|http)\b[^\n]{0,10}\b429\b", re.IGNORECASE),
    re.compile(r"\b429\b[^\n]{0,20}\btoo many requests\b", re.IGNORECASE),
]

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR.parent / ".env"  # skills/gnhf/.env, not cwd-relative


def load_config(env_file):
    """Build a decouple Config that reads process env, then env_file if it
    exists. Deliberately not decouple's AutoConfig singleton -- that searches
    upward from cwd for a .env, which would pick up an unrelated one from
    whatever task worktree this script happens to be running in."""
    if env_file is not None and Path(env_file).exists():
        return Config(RepositoryEnv(str(env_file)))
    return Config(RepositoryEmpty())


config = load_config(ENV_FILE)

TTL_DEFAULT = config("GNHF_TTL", default=10800, cast=int)
PROBE_DEFAULT = config("GNHF_PROBE", default=25, cast=float)
BASE_BACKOFF_DEFAULT = config("GNHF_BASE_BACKOFF", default=75, cast=float)
MAX_BACKOFF_DEFAULT = config("GNHF_MAX_BACKOFF", default=900, cast=float)
MAX_429_DEFAULT = config("GNHF_MAX_429", default=6, cast=int)
TOTAL_BACKOFF_CAP_DEFAULT = config("GNHF_TOTAL_BACKOFF_CAP", default=2700, cast=float)
TIMEOUT_DEFAULT = config("GNHF_TIMEOUT", default=300, cast=int)
MAX_RETRIES_DEFAULT = config("GNHF_MAX_RETRIES", default=2, cast=int)
PROVIDER_DEFAULT = config("GNHF_PROVIDER", default=None)
MODEL_DEFAULT = config("GNHF_MODEL", default=None)

PROBE_POLL_INTERVAL = 0.05


def is_rate_limited(text):
    return any(pattern.search(text) for pattern in RATE_LIMIT_PATTERNS)


def _raw_backoff(attempt, base, cap):
    return min(base * (2 ** (attempt - 1)), cap)


def backoff_delay(attempt, base, cap):
    return _raw_backoff(attempt, base, cap) * random.uniform(0.8, 1.2)


def print_tail(text, n):
    for line in text.splitlines()[-n:]:
        print(line, file=sys.stderr)


# Adapted from ~/git/timecard/opencode.jsonc's example policy. Every `ask`
# in that reference becomes `deny` here: there is no human to answer an
# unattended prompt, and a silently-hung run wastes the rest of its TTL
# worse than a hard deny would. See spec "Default gnhf permission ruleset".
_BASH_DENY_PATTERNS = [
    "rm -rf /*", "rm -rf /", "rm *",
    "sudo *",
    "dd *", "mkfs *", "fdisk *", "parted *",
    "diskutil eraseDisk*", "diskutil eraseVolume*",
    "diskutil partitionDisk*", "diskutil apfs deleteContainer*", "diskutil *",
    "newfs*", "mount *", "umount *",
    "shutdown *", "reboot *", "halt *",
    "nvram *", "bless *", "csrutil *", "systemsetup *",
    "launchctl *", "networksetup *", "scutil *", "dscl *", "pmset *",
    "tmutil delete*", "tmutil *",
    "git push --force*", "git reset --hard*",
    "git checkout *", "git switch *",
    "killall *", "pkill *",
    "curl * | *sh*", "wget * | *sh*",
]


def build_gnhf_permission_ruleset():
    ruleset = [
        {"permission": "edit", "pattern": "*", "action": "allow"},
        {"permission": "webfetch", "pattern": "*", "action": "allow"},
        {"permission": "bash", "pattern": "*", "action": "allow"},
    ]
    ruleset += [
        {"permission": "bash", "pattern": pattern, "action": "deny"}
        for pattern in _BASH_DENY_PATTERNS
    ]
    return ruleset


SERVE_READY_TIMEOUT = 15
SERVE_LISTENING_RE = re.compile(r"opencode server listening on http://[^:]+:(\d+)")
SERVE_POLL_INTERVAL = 0.1


def start_opencode_serve(cwd, log_path):
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logf = open(log_path, "a")
    proc = subprocess.Popen(
        ["opencode", "serve", "--port", "0", "--hostname", "127.0.0.1"],
        cwd=cwd, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True,
    )
    deadline = time.monotonic() + SERVE_READY_TIMEOUT
    while time.monotonic() < deadline:
        text = log_path.read_text(errors="replace")
        match = SERVE_LISTENING_RE.search(text)
        if match:
            return proc, int(match.group(1))
        if proc.poll() is not None:
            raise RuntimeError(
                f"opencode serve exited (code {proc.returncode}) before listening; "
                f"see {log_path}"
            )
        time.sleep(SERVE_POLL_INTERVAL)
    raise RuntimeError(f"opencode serve did not print a listening line within {SERVE_READY_TIMEOUT}s; see {log_path}")


def parse_args(argv):
    if "--" in argv:
        idx = argv.index("--")
        main_argv, launch_cmd = argv[:idx], argv[idx + 1:]
    else:
        main_argv, launch_cmd = argv, []

    parser = argparse.ArgumentParser(
        prog="gnhf.py",
        description="Smoke-test or launch an agent CLI, surviving 429 "
        "concurrency-limit contention instead of failing on it.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("-s", "--smoke-test", metavar="AGENT", choices=["pi", "opencode"])
    mode.add_argument("-l", "--launch", action="store_true")
    mode.add_argument("--poll", metavar="STATE_FILE")

    parser.add_argument("--agent", choices=["pi", "opencode"])

    # smoke-test options
    parser.add_argument("-P", "--provider", default=PROVIDER_DEFAULT)
    parser.add_argument("-m", "--model", default=MODEL_DEFAULT)
    parser.add_argument("-d", "--path", default=None)
    parser.add_argument("-t", "--timeout", type=int, default=TIMEOUT_DEFAULT)
    parser.add_argument("-r", "--max-retries", type=int, default=MAX_RETRIES_DEFAULT)

    # launch options
    parser.add_argument("-C", "--cwd", default=None)
    parser.add_argument("-o", "--log", default=None)
    parser.add_argument("-T", "--ttl", type=int, default=TTL_DEFAULT)
    parser.add_argument("-p", "--probe", type=float, default=PROBE_DEFAULT)
    parser.add_argument("-b", "--base-backoff", type=float, default=BASE_BACKOFF_DEFAULT)
    parser.add_argument("-B", "--max-backoff", type=float, default=MAX_BACKOFF_DEFAULT)
    parser.add_argument("-n", "--max-429", type=int, default=MAX_429_DEFAULT)
    parser.add_argument("-c", "--total-backoff-cap", type=float, default=TOTAL_BACKOFF_CAP_DEFAULT)
    parser.add_argument("--herdr-pane", dest="herdr_pane", action="store_true", default=None)
    parser.add_argument("--no-herdr-pane", dest="herdr_pane", action="store_false")

    args = parser.parse_args(main_argv)
    args.launch_cmd = launch_cmd

    if args.launch:
        if not args.agent:
            parser.error("--launch requires --agent pi|opencode")
        if not args.cwd or not args.log:
            parser.error("--launch requires --cwd and --log")
        if args.agent == "pi" and not launch_cmd:
            parser.error("--launch --agent pi requires a command after --")
        if args.agent == "opencode" and launch_cmd:
            parser.error("--launch --agent opencode does not take a trailing command "
                          "(it drives the server API, not a foreground process)")

    if args.smoke_test == "pi" and args.agent and args.agent != "pi":
        parser.error("--agent must match --smoke-test's AGENT")

    return args


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    print("gnhf.py scaffolding only -- modes not yet implemented", file=sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
