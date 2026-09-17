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


API_TIMEOUT = 30  # a single HTTP round-trip, not the agent's own turn -- prompt_async
                   # returns in well under a second (confirmed live: ~0.1s)

# Ported from rpc-bridge.py's MARKER_RE, minus the ASK alternative -- gnhf.py
# via the server API has no live-ask channel to match against.
MARKER_RE = re.compile(
    r"^MANUAL_RUN:\s*(DONE|BAILED)\b\s*(?:[-–—:]+\s*)?(.*?)"
    r"(?=\n*^MANUAL_RUN:\s*(?:DONE|BAILED)\b|\Z)",
    re.MULTILINE | re.DOTALL,
)


def api_create_session(base_url, permission, directory=None):
    body = {"permission": permission}
    if directory is not None:
        body["directory"] = directory
    response = requests.post(f"{base_url}/session", json=body, timeout=API_TIMEOUT)
    response.raise_for_status()
    return response.json()["id"]


def api_prompt_async(base_url, session_id, text):
    response = requests.post(
        f"{base_url}/session/{session_id}/prompt_async",
        json={"parts": [{"type": "text", "text": text}]},
        timeout=API_TIMEOUT,
    )
    response.raise_for_status()


def api_get_messages(base_url, session_id):
    response = requests.get(f"{base_url}/session/{session_id}/message", timeout=API_TIMEOUT)
    response.raise_for_status()
    return response.json()


def api_list_permissions(base_url, session_id):
    response = requests.get(f"{base_url}/session/{session_id}/permission", timeout=API_TIMEOUT)
    response.raise_for_status()
    return response.json()


def api_reject_permission(base_url, session_id, request_id):
    response = requests.post(
        f"{base_url}/session/{session_id}/permission/{request_id}/reply",
        json={"reply": "reject"},
        timeout=API_TIMEOUT,
    )
    response.raise_for_status()


def api_abort_session(base_url, session_id):
    response = requests.post(f"{base_url}/session/{session_id}/abort", timeout=API_TIMEOUT)
    response.raise_for_status()


def write_state_file(path, *, base_url, session_id, server_pid, worktree, launched_at, ttl):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "base_url": base_url,
        "session_id": session_id,
        "server_pid": server_pid,
        "worktree": worktree,
        "launched_at": launched_at,
        "ttl": ttl,
    }))


def read_state_file(path):
    return json.loads(Path(path).read_text())


def find_manual_run_marker(messages):
    for message in reversed(messages):
        info = message.get("info", {})
        if info.get("role") != "assistant":
            continue
        if not info.get("time", {}).get("completed"):
            continue
        text = "\n".join(
            part.get("text", "") for part in message.get("parts", []) if part.get("type") == "text"
        )
        match = MARKER_RE.search(text)
        if match:
            return match.group(1), match.group(2).strip()
        return None
    return None


def process_alive(pid):
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return pid_error_means_dead(pid)
    return True


def pid_error_means_dead(pid):
    # ProcessLookupError: definitely gone. PermissionError: exists but owned
    # by someone else -- treat as alive, since gnhf only ever signals PIDs
    # it started itself.
    return False


def run_poll(state_path):
    state = read_state_file(state_path)
    base_url, session_id = state["base_url"], state["session_id"]

    if not process_alive(state["server_pid"]):
        print(f"SERVER_DIED: opencode serve (pid {state['server_pid']}) is no longer running")
        return EXIT_FAIL

    launched_at = datetime.fromisoformat(state["launched_at"])
    now = datetime.now(launched_at.tzinfo)
    if (now - launched_at).total_seconds() >= state["ttl"]:
        api_abort_session(base_url, session_id)
        print(f"TTL_EXPIRED: aborted session {session_id} after {state['ttl']}s")
        return EXIT_OK

    for pending in api_list_permissions(base_url, session_id):
        api_reject_permission(base_url, session_id, pending["id"])

    messages = api_get_messages(base_url, session_id)
    marker = find_manual_run_marker(messages)
    if marker is not None:
        kind, detail = marker
        print(f"MANUAL_RUN: {kind} — {detail}")
        return EXIT_OK

    print("RUNNING")
    return EXIT_OK


def maybe_open_herdr_pane(worktree, task_label, url):
    """If HERDR_ENV=1, opens a Herdr workspace pane running
    `opencode attach <url>` so the run is watchable as a real interactive
    TUI -- matches SKILL.md's existing Herdr convention for other agents.
    A pane failure is a visibility-only degradation, never fatal: the
    server + session automation is the load-bearing path either way."""
    if os.environ.get("HERDR_ENV") != "1":
        return
    if not shutil.which("herdr"):
        return
    try:
        ws = subprocess.run(
            ["herdr", "workspace", "create", "--cwd", worktree, "--label", task_label, "--no-focus"],
            capture_output=True, text=True, timeout=15, check=True,
        )
        pane_id = json.loads(ws.stdout)["result"]["root_pane"]["pane_id"]
        subprocess.run(
            ["herdr", "pane", "run", pane_id, f"opencode attach {url}"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as exc:
        print(f"NOTE: Herdr pane setup failed ({exc}); continuing without a visible TUI", file=sys.stderr)


def run_launch_opencode(cwd, log_path, ttl, probe, base_backoff, max_backoff,
                         max_429, total_backoff_cap, prompt, herdr_pane):
    log_path = Path(log_path)
    state_path = log_path.with_suffix(".state.json")
    task_label = log_path.stem

    attempt = 0
    total_backoff = 0.0

    while True:
        attempt += 1
        try:
            proc, port = start_opencode_serve(cwd, log_path)
        except RuntimeError as exc:
            print(f"EARLY_EXIT: {exc}", file=sys.stderr)
            return EXIT_EARLY_EXIT

        base_url = f"http://127.0.0.1:{port}"
        rate_limited = False
        early_exit_reason = None
        session_id = None

        try:
            ruleset = build_gnhf_permission_ruleset()
            session_id = api_create_session(base_url, ruleset)
            api_prompt_async(base_url, session_id, prompt)
        except requests.exceptions.RequestException as exc:
            if is_rate_limited(str(exc)):
                rate_limited = True
            else:
                early_exit_reason = str(exc)

        if not rate_limited and early_exit_reason is None:
            deadline = time.monotonic() + probe
            while time.monotonic() < deadline:
                time.sleep(PROBE_POLL_INTERVAL)
                if proc.poll() is not None:
                    early_exit_reason = f"opencode serve exited (code {proc.returncode}) during probe window"
                    break
                try:
                    messages = api_get_messages(base_url, session_id)
                except requests.exceptions.RequestException:
                    continue
                text = json.dumps(messages)
                if is_rate_limited(text):
                    rate_limited = True
                    break

        if rate_limited:
            with contextlib.suppress(Exception):
                proc.terminate()
                proc.wait(timeout=5)
            if attempt >= max_429 or total_backoff >= total_backoff_cap:
                print(f"RATE_LIMITED: gave up after {attempt} attempts, {total_backoff:.0f}s of backoff", file=sys.stderr)
                return EXIT_RATE_LIMITED
            delay = backoff_delay(attempt, base_backoff, max_backoff)
            total_backoff += delay
            print(f"RATE_LIMITED: attempt {attempt}, backing off {delay:.0f}s", file=sys.stderr)
            time.sleep(delay)
            continue

        if early_exit_reason:
            with contextlib.suppress(Exception):
                proc.terminate()
                proc.wait(timeout=5)
            print(f"EARLY_EXIT: {early_exit_reason}", file=sys.stderr)
            return EXIT_EARLY_EXIT

        launched_at = datetime.now().astimezone().isoformat()
        write_state_file(
            state_path, base_url=base_url, session_id=session_id, server_pid=proc.pid,
            worktree=cwd, launched_at=launched_at, ttl=ttl,
        )
        if herdr_pane:
            maybe_open_herdr_pane(cwd, task_label, base_url)

        ttl_expires = (datetime.now() + timedelta(seconds=ttl)).isoformat()
        print(f"LAUNCHED: session={session_id} port={port} pid={proc.pid} state={state_path} ttl_expires={ttl_expires}")
        return EXIT_OK


def _kill_process_group(proc):
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)


def run_launch_pi(cwd, log_path, ttl, probe, base_backoff, max_backoff, max_429, total_backoff_cap, command):
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    attempt = 0
    total_backoff = 0.0

    with open(log_path, "a") as logf:
        while True:
            attempt += 1
            logf.write(f"\n--- gnhf launch attempt {attempt} at {datetime.now().isoformat()} ---\n")
            logf.flush()
            attempt_offset = logf.tell()

            full_cmd = ["timeout", str(ttl), *command]
            proc = subprocess.Popen(full_cmd, cwd=cwd, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)

            def attempt_output():
                with open(log_path, errors="replace") as f:
                    f.seek(attempt_offset)
                    return f.read()

            rate_limited = False
            exited_early = False
            deadline = time.monotonic() + probe
            while time.monotonic() < deadline:
                time.sleep(PROBE_POLL_INTERVAL)
                if is_rate_limited(attempt_output()):
                    rate_limited = True
                    break
                if proc.poll() is not None:
                    exited_early = True
                    break

            if not rate_limited and not exited_early and proc.poll() is not None:
                if is_rate_limited(attempt_output()):
                    rate_limited = True
                else:
                    exited_early = True

            if rate_limited:
                _kill_process_group(proc)
                if attempt >= max_429 or total_backoff >= total_backoff_cap:
                    print(f"RATE_LIMITED: gave up after {attempt} attempts, {total_backoff:.0f}s of backoff", file=sys.stderr)
                    return EXIT_RATE_LIMITED
                delay = backoff_delay(attempt, base_backoff, max_backoff)
                total_backoff += delay
                print(f"RATE_LIMITED: attempt {attempt}, backing off {delay:.0f}s", file=sys.stderr)
                time.sleep(delay)
                continue

            if exited_early:
                print(f"EARLY_EXIT: process exited during the {probe}s probe window, not rate-limited -- a real dispatch failure", file=sys.stderr)
                return EXIT_EARLY_EXIT

            ttl_expires = (datetime.now() + timedelta(seconds=max(ttl - probe, 0))).isoformat()
            print(f"LAUNCHED: pid={proc.pid} attempt={attempt} ttl_expires={ttl_expires}")
            return EXIT_OK


def build_smoke_cmd(agent, provider, model, path):
    if agent == "pi":
        if not shutil.which("pi"):
            return None, "FAIL: pi is not on PATH"
        cmd = ["pi"]
        if provider:
            cmd += ["--provider", provider]
        if model:
            cmd += ["--model", model]
        cmd += ["-p", SMOKE_TEST_PROMPT, "--no-session"]
        return cmd, None
    return None, f"Unknown agent for build_smoke_cmd: {agent}"


def run_with_timeout(cmd, timeout_s):
    start = time.monotonic()
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout_s, text=True)
        rc, output = proc.returncode, proc.stdout
    except subprocess.TimeoutExpired as exc:
        rc = 124
        output = exc.output if isinstance(exc.output, str) else (exc.output or b"").decode(errors="replace")
    return rc, time.monotonic() - start, output


def run_smoke_test(agent, provider, model, path, timeout_s, max_retries, base_backoff, max_backoff):
    cmd, err = build_smoke_cmd(agent, provider, model, path or os.getcwd())
    if cmd is None:
        print(err, file=sys.stderr)
        return EXIT_FAIL

    attempt = 0
    while True:
        attempt += 1
        print(f"Running: {' '.join(cmd)} (timeout {timeout_s}s)", file=sys.stderr)
        rc, elapsed, output = run_with_timeout(cmd, timeout_s)

        if rc == 124:
            print(f"FAIL: timed out after {timeout_s}s waiting for a response", file=sys.stderr)
            print_tail(output, 40)
            return EXIT_FAIL

        if rc != 0:
            if is_rate_limited(output):
                if attempt > max_retries:
                    print(f"RATE_LIMITED: {agent} still rate-limited after {attempt} attempts", file=sys.stderr)
                    print_tail(output, 60)
                    return EXIT_RATE_LIMITED
                delay = backoff_delay(attempt, base_backoff, max_backoff)
                print(f"RATE_LIMITED: attempt {attempt}, retrying in {delay:.0f}s", file=sys.stderr)
                time.sleep(delay)
                continue
            print(f"FAIL: {agent} exited with status {rc} after {elapsed:.0f}s", file=sys.stderr)
            print_tail(output, 60)
            return EXIT_FAIL

        if re.search(r"pong", output, re.IGNORECASE):
            print(f"PASS: {agent} responded correctly in {elapsed:.0f}s")
            return EXIT_OK

        print(f"FAIL: {agent} ran without error but did not return the expected reply", file=sys.stderr)
        print_tail(output, 60)
        return EXIT_FAIL


def run_smoke_test_opencode(provider, model, timeout_s, max_retries):
    import tempfile
    with tempfile.TemporaryDirectory(prefix="gnhf-smoke-") as tmp:
        log_path = Path(tmp) / "smoke.log"
        try:
            proc, port = start_opencode_serve(tmp, log_path)
        except RuntimeError as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return EXIT_FAIL

        base_url = f"http://127.0.0.1:{port}"
        try:
            start = time.monotonic()
            session_id = api_create_session(base_url, [])
            api_prompt_async(base_url, session_id, SMOKE_TEST_PROMPT)

            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                messages = api_get_messages(base_url, session_id)
                for message in reversed(messages):
                    info = message.get("info", {})
                    if info.get("role") == "assistant" and info.get("time", {}).get("completed"):
                        text = "\n".join(
                            p.get("text", "") for p in message.get("parts", []) if p.get("type") == "text"
                        )
                        elapsed = time.monotonic() - start
                        if re.search(r"pong", text, re.IGNORECASE):
                            print(f"PASS: opencode responded correctly in {elapsed:.0f}s")
                            return EXIT_OK
                        print(f"FAIL: opencode ran without error but did not return the expected reply", file=sys.stderr)
                        print(f"--- output ---\n{text}", file=sys.stderr)
                        return EXIT_FAIL
                time.sleep(PROBE_POLL_INTERVAL)
            print(f"FAIL: timed out after {timeout_s}s waiting for a response", file=sys.stderr)
            return EXIT_FAIL
        finally:
            with contextlib.suppress(Exception):
                proc.terminate()
                proc.wait(timeout=5)


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

    if args.poll:
        return run_poll(args.poll)

    if args.launch:
        if args.agent == "pi":
            return run_launch_pi(
                cwd=args.cwd, log_path=args.log, ttl=args.ttl, probe=args.probe,
                base_backoff=args.base_backoff, max_backoff=args.max_backoff,
                max_429=args.max_429, total_backoff_cap=args.total_backoff_cap,
                command=args.launch_cmd,
            )
        prompt_path = Path(args.cwd) / ".gnhf-prompt.md"
        prompt = prompt_path.read_text() if prompt_path.exists() else ""
        herdr_pane = args.herdr_pane if args.herdr_pane is not None else os.environ.get("HERDR_ENV") == "1"
        return run_launch_opencode(
            cwd=args.cwd, log_path=args.log, ttl=args.ttl, probe=args.probe,
            base_backoff=args.base_backoff, max_backoff=args.max_backoff,
            max_429=args.max_429, total_backoff_cap=args.total_backoff_cap,
            prompt=prompt, herdr_pane=herdr_pane,
        )

    if args.smoke_test == "opencode":
        return run_smoke_test_opencode(args.provider, args.model, args.timeout, args.max_retries)

    return run_smoke_test(
        agent=args.smoke_test, provider=args.provider, model=args.model, path=args.path,
        timeout_s=args.timeout, max_retries=args.max_retries,
        base_backoff=BASE_BACKOFF_DEFAULT, max_backoff=MAX_BACKOFF_DEFAULT,
    )


if __name__ == "__main__":
    sys.exit(main())
