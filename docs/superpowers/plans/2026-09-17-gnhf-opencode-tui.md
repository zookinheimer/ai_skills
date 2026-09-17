# gnhf: opencode-TUI-driven unattended runs — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace gnhf's pi-specific `rpc-bridge.py` + plan-mode extension with a single `scripts/gnhf.py` that drives opencode through its server REST API (session-scoped permissions, live TUI as just another API client, TTL via clean session abort), making opencode + a Herdr-visible TUI the default unattended-run path while keeping `pi` as a secondary, smoke-test-only agent.

**Architecture:** `opencode serve` runs detached in the target worktree; a Herdr pane runs `opencode attach <url>` for a human-visible, genuinely interactive TUI; `gnhf.py` talks to the same server's HTTP API to create the session (with a gnhf-specific permission ruleset), send the prompt, poll for completion markers and pending permissions, and enforce the TTL via a clean session abort. All three (TUI, server, gnhf.py) are independent clients of one session — no screen-scraping anywhere.

**Tech Stack:** Python 3.13, `uv run --script` (PEP 723) single-file tool, `python-decouple` (config layering, unchanged from upstream), `requests` (new — HTTP calls to the opencode server API), `pytest` (test suite, mocked HTTP — no live server in CI).

**Spec:** `docs/superpowers/specs/2026-09-17-gnhf-opencode-tui-design.md`

## Global Constraints

- Every tunable resolves through `python-decouple`: CLI flag > process env > `skills/gnhf/.env` > hardcoded default (spec, upstream-inherited).
- The default gnhf permission ruleset must contain **zero** `"ask"` actions — every `ask` in the timecard reference policy becomes `deny` (spec: "Default gnhf permission ruleset").
- `pi`'s existing headless `pi -p` launch/smoke-test path is preserved unchanged — this is additive for opencode, not a pi removal (spec: "Component 1").
- No screen-scraping of TUI output anywhere in `gnhf.py` — all opencode interaction goes through the documented REST API (spec: "Key discovery").
- `MANUAL_RUN: DONE —` / `MANUAL_RUN: BAILED —` marker convention and its tolerant-separator matching (ported from `rpc-bridge.py`'s `MARKER_RE`) are unchanged — this is what SKILL.md's step 4 prompt template already asks the model to print.

---

## File Structure

- **Create** `skills/gnhf/scripts/gnhf.py` — the new unified tool (smoke-test, launch, poll modes for both agents).
- **Create** `skills/gnhf/scripts/test_gnhf.py` — pytest suite, all HTTP mocked.
- **Create** `skills/gnhf/.env.example` — config override template (ported from upstream, same `GNHF_*` names).
- **Delete** `skills/gnhf/scripts/rpc-bridge.py`, `skills/gnhf/scripts/rpc-bridge-smoke-test.sh`, `skills/gnhf/scripts/smoke-test.sh`, `skills/gnhf/scripts/pi-extensions/plan-mode/` (whole directory) — all superseded by `gnhf.py` + opencode's native permission system.
- **Modify** `skills/gnhf/scripts/mcp-defaults.json` — reshape from pi's `mcpServers` convention to the list of `POST /mcp` request bodies `gnhf.py` will send to a freshly started `opencode serve`.
- **Modify** `skills/gnhf/SKILL.md` — steps 1 (tie-break default), 5 (launch), 6 (monitor/poll), 7 (finish — minimal wording only), "Bundled scripts".
- **Modify** `skills/gnhf/README.md` — Quickstart table, Parameters, new env var table, TUI/Herdr visibility note.
- **Modify** `README.md` (repo root) — drop the stale "isn't merged to main yet" `gnhf-pi-rpc-bridge` branch language (that content has been on `main` since PRs #1-#13); describe opencode+TUI as the default agent.

---

## Task 1: `gnhf.py` scaffolding — CLI modes and config loading

**Files:**
- Create: `skills/gnhf/scripts/gnhf.py`
- Test: `skills/gnhf/scripts/test_gnhf.py`

**Interfaces:**
- Produces: `parse_args(argv) -> argparse.Namespace` with `.smoke_test: str|None`, `.launch: bool`, `.poll: str|None` (state-file path), `.agent: str` (`"pi"|"opencode"`), plus all launch/smoke-test tunables as upstream (`--ttl`, `--probe`, `--base-backoff`, `--max-backoff`, `--max-429`, `--total-backoff-cap`, `--timeout`, `--max-retries`, `--provider`, `--model`, `--cwd`, `--log`), and `.launch_cmd: list[str]` (only used when `--agent pi`, everything after `--`).
- Produces: module-level config constants (`TTL_DEFAULT`, `PROBE_DEFAULT`, `BASE_BACKOFF_DEFAULT`, `MAX_BACKOFF_DEFAULT`, `MAX_429_DEFAULT`, `TOTAL_BACKOFF_CAP_DEFAULT`, `TIMEOUT_DEFAULT`, `MAX_RETRIES_DEFAULT`, `PROVIDER_DEFAULT`, `MODEL_DEFAULT`) resolved through `python-decouple`, identical names/defaults to upstream's `gnhf.py`.
- Produces: exit code constants `EXIT_OK=0`, `EXIT_FAIL=1`, `EXIT_USAGE=2`, `EXIT_RATE_LIMITED=3`, `EXIT_EARLY_EXIT=4` (upstream-identical).

- [ ] **Step 1: Write the failing tests for arg parsing**

```python
# skills/gnhf/scripts/test_gnhf.py
import subprocess
import sys
from pathlib import Path

import pytest

GNHF = Path(__file__).parent / "gnhf.py"


def run_gnhf(*args):
    return subprocess.run(
        [sys.executable, str(GNHF), *args],
        capture_output=True, text=True,
    )


def test_requires_a_mode():
    result = run_gnhf()
    assert result.returncode == 2
    assert "required" in result.stderr.lower() or "one of the arguments" in result.stderr.lower()


def test_smoke_test_rejects_unknown_agent():
    result = run_gnhf("-s", "codex")
    assert result.returncode == 2


def test_launch_requires_cwd_and_log():
    result = run_gnhf("-l", "--agent", "opencode")
    assert result.returncode == 2
    assert "--cwd" in result.stderr or "cwd" in result.stderr.lower()


def test_launch_pi_requires_trailing_command():
    result = run_gnhf("-l", "--agent", "pi", "-C", "/tmp", "-o", "/tmp/x.log")
    assert result.returncode == 2


def test_launch_opencode_rejects_trailing_command():
    # opencode launches drive the server API, not a foreground command --
    # a `-- <command>` tail is a pi-only concept and must be rejected early.
    result = run_gnhf(
        "-l", "--agent", "opencode", "-C", "/tmp", "-o", "/tmp/x.log",
        "--", "echo", "hi",
    )
    assert result.returncode == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd skills/gnhf/scripts && uv run --script test_gnhf.py -k "test_requires_a_mode or test_smoke_test_rejects_unknown_agent or test_launch_requires_cwd_and_log or test_launch_pi_requires_trailing_command or test_launch_opencode_rejects_trailing_command" -v`
Expected: FAIL — `gnhf.py` doesn't exist yet (`FileNotFoundError` / non-zero from the harness itself).

- [ ] **Step 3: Write `gnhf.py`'s scaffolding**

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd skills/gnhf/scripts && uv run --script test_gnhf.py -k "test_requires_a_mode or test_smoke_test_rejects_unknown_agent or test_launch_requires_cwd_and_log or test_launch_pi_requires_trailing_command or test_launch_opencode_rejects_trailing_command" -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add skills/gnhf/scripts/gnhf.py skills/gnhf/scripts/test_gnhf.py
git commit -m "feat(gnhf): scaffold gnhf.py CLI modes and config loading"
```

---

## Task 2: Port rate-limit detection and backoff (upstream-identical)

**Files:**
- Modify: `skills/gnhf/scripts/gnhf.py`
- Test: `skills/gnhf/scripts/test_gnhf.py`

**Interfaces:**
- Consumes: nothing new from Task 1 beyond the module already existing.
- Produces: `is_rate_limited(text: str) -> bool`, `backoff_delay(attempt: int, base: float, cap: float) -> float`, `_raw_backoff(attempt: int, base: float, cap: float) -> float`, `print_tail(text: str, n: int) -> None`. Later tasks call `is_rate_limited` and `backoff_delay` directly.

- [ ] **Step 1: Write the failing tests**

```python
def test_is_rate_limited_matches_known_patterns():
    from gnhf import is_rate_limited
    assert is_rate_limited('{"code": "concurrency_limit"}')
    assert is_rate_limited("rate_limit_error: too many requests")
    assert is_rate_limited("HTTP status 429")
    assert is_rate_limited("429 too many requests")
    assert not is_rate_limited("PONG")


def test_backoff_delay_grows_and_caps():
    from gnhf import _raw_backoff
    assert _raw_backoff(1, base=75, cap=900) == 75
    assert _raw_backoff(2, base=75, cap=900) == 150
    assert _raw_backoff(10, base=75, cap=900) == 900  # capped
```

(Import note: since `gnhf.py` is a `uv run --script` file, not a package,
`test_gnhf.py` adds its directory to `sys.path` before importing — add this
once near the top of `test_gnhf.py`, above the existing test functions:)

```python
sys.path.insert(0, str(Path(__file__).parent))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd skills/gnhf/scripts && uv run --script test_gnhf.py -k "test_is_rate_limited_matches_known_patterns or test_backoff_delay_grows_and_caps" -v`
Expected: FAIL — `ImportError: cannot import name 'is_rate_limited'`

- [ ] **Step 3: Add the functions to `gnhf.py`** (insert after the `config = load_config(...)` / constant block, before `parse_args`)

```python
def is_rate_limited(text):
    return any(pattern.search(text) for pattern in RATE_LIMIT_PATTERNS)


def _raw_backoff(attempt, base, cap):
    return min(base * (2 ** (attempt - 1)), cap)


def backoff_delay(attempt, base, cap):
    return _raw_backoff(attempt, base, cap) * random.uniform(0.8, 1.2)


def print_tail(text, n):
    for line in text.splitlines()[-n:]:
        print(line, file=sys.stderr)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd skills/gnhf/scripts && uv run --script test_gnhf.py -k "test_is_rate_limited_matches_known_patterns or test_backoff_delay_grows_and_caps" -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add skills/gnhf/scripts/gnhf.py skills/gnhf/scripts/test_gnhf.py
git commit -m "feat(gnhf): port rate-limit detection and backoff from upstream"
```

---

## Task 3: Default gnhf permission ruleset

**Files:**
- Modify: `skills/gnhf/scripts/gnhf.py`
- Test: `skills/gnhf/scripts/test_gnhf.py`

**Interfaces:**
- Produces: `build_gnhf_permission_ruleset() -> list[dict]`, each dict shaped `{"permission": str, "pattern": str, "action": "allow"|"deny"}` (opencode's `PermissionRule`, confirmed live against a running `opencode serve /doc`: `permission`/`pattern`/`action` all required, `action` is `"allow"|"deny"|"ask"`). Consumed by Task 6's `api_create_session`.

- [ ] **Step 1: Write the failing tests**

```python
def test_permission_ruleset_has_no_ask_actions():
    from gnhf import build_gnhf_permission_ruleset
    ruleset = build_gnhf_permission_ruleset()
    assert all(rule["action"] in ("allow", "deny") for rule in ruleset)
    assert not any(rule["action"] == "ask" for rule in ruleset)


def test_permission_ruleset_denies_destructive_bash():
    from gnhf import build_gnhf_permission_ruleset
    ruleset = build_gnhf_permission_ruleset()
    deny_patterns = {r["pattern"] for r in ruleset if r["permission"] == "bash" and r["action"] == "deny"}
    for expected in ("rm -rf /*", "sudo *", "dd *", "git push --force*",
                     "git reset --hard*", "git checkout *", "git switch *",
                     "shutdown *", "killall *"):
        assert expected in deny_patterns, f"missing deny pattern: {expected}"


def test_permission_ruleset_allows_edit_and_webfetch():
    from gnhf import build_gnhf_permission_ruleset
    ruleset = build_gnhf_permission_ruleset()
    by_permission = {r["permission"]: r for r in ruleset if r["pattern"] == "*"}
    assert by_permission["edit"]["action"] == "allow"
    assert by_permission["webfetch"]["action"] == "allow"
    assert by_permission["bash"]["action"] == "allow"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd skills/gnhf/scripts && uv run --script test_gnhf.py -k "test_permission_ruleset" -v`
Expected: FAIL — `ImportError: cannot import name 'build_gnhf_permission_ruleset'`

- [ ] **Step 3: Add the ruleset builder to `gnhf.py`** (insert after the backoff helpers from Task 2)

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd skills/gnhf/scripts && uv run --script test_gnhf.py -k "test_permission_ruleset" -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add skills/gnhf/scripts/gnhf.py skills/gnhf/scripts/test_gnhf.py
git commit -m "feat(gnhf): add default gnhf permission ruleset for opencode sessions"
```

---

## Task 4: opencode server process lifecycle

**Files:**
- Modify: `skills/gnhf/scripts/gnhf.py`
- Test: `skills/gnhf/scripts/test_gnhf.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `start_opencode_serve(cwd: str, log_path: Path) -> tuple[subprocess.Popen, int]` (starts `opencode serve --port 0 --hostname 127.0.0.1` detached with `cwd=cwd`, `stdout=stderr=log_path`, `start_new_session=True`; parses the bound port from the log's `opencode server listening on http://127.0.0.1:<port>` line — confirmed live, this exact string, port assigned even when `--port 0` is passed; raises `RuntimeError` if the line doesn't appear within `SERVE_READY_TIMEOUT` seconds). Produces module constant `SERVE_READY_TIMEOUT = 15`. Consumed by Task 6's launch orchestration and Task 8's smoke-test mode.

- [ ] **Step 1: Write the failing tests**

```python
def test_start_opencode_serve_parses_bound_port(tmp_path, monkeypatch):
    from gnhf import start_opencode_serve

    log_path = tmp_path / "serve.log"

    class FakeProc:
        def poll(self):
            return None

        pid = 4242

    def fake_popen(cmd, **kwargs):
        assert cmd[:2] == ["opencode", "serve"]
        log_path.write_text(
            "Warning: OPENCODE_SERVER_PASSWORD is not set; server is unsecured.\n"
            "opencode server listening on http://127.0.0.1:54321\n"
        )
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    proc, port = start_opencode_serve(str(tmp_path), log_path)
    assert port == 54321
    assert proc.pid == 4242


def test_start_opencode_serve_times_out_if_no_listening_line(tmp_path, monkeypatch):
    from gnhf import start_opencode_serve

    log_path = tmp_path / "serve.log"
    log_path.write_text("")

    class FakeProc:
        def poll(self):
            return None
        pid = 1

    monkeypatch.setattr("subprocess.Popen", lambda cmd, **kwargs: FakeProc())
    monkeypatch.setattr("gnhf.SERVE_READY_TIMEOUT", 0.2)
    with pytest.raises(RuntimeError, match="listening"):
        start_opencode_serve(str(tmp_path), log_path)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd skills/gnhf/scripts && uv run --script test_gnhf.py -k "test_start_opencode_serve" -v`
Expected: FAIL — `ImportError: cannot import name 'start_opencode_serve'`

- [ ] **Step 3: Add the lifecycle helper to `gnhf.py`** (insert after Task 3's ruleset builder)

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd skills/gnhf/scripts && uv run --script test_gnhf.py -k "test_start_opencode_serve" -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add skills/gnhf/scripts/gnhf.py skills/gnhf/scripts/test_gnhf.py
git commit -m "feat(gnhf): start and detect readiness of a detached opencode serve"
```

---

## Task 5: opencode session API client

**Files:**
- Modify: `skills/gnhf/scripts/gnhf.py`
- Test: `skills/gnhf/scripts/test_gnhf.py`

**Interfaces:**
- Consumes: `requests` (module-level import from Task 1).
- Produces:
  - `api_create_session(base_url: str, permission: list[dict], directory: str | None = None) -> str` — `POST {base_url}/session` with JSON body `{"permission": permission}` (plus `"directory": directory` if given — confirmed live that omitting it makes the session inherit the server process's own `cwd`, so gnhf.py never needs to pass it since `start_opencode_serve` already launches with `cwd=worktree`). Returns `response.json()["id"]`.
  - `api_prompt_async(base_url: str, session_id: str, text: str) -> None` — `POST {base_url}/session/{session_id}/prompt_async` with `{"parts": [{"type": "text", "text": text}]}`. Confirmed live: returns HTTP 204 immediately, does not block on the model turn.
  - `api_get_messages(base_url: str, session_id: str) -> list[dict]` — `GET {base_url}/session/{session_id}/message`. Returns the raw list of `{"info": {...}, "parts": [...]}` objects (confirmed live shape: `info.role` is `"user"`/`"assistant"`, a finished assistant message has `info.time.completed` set, and its reply text lives in `parts[]` entries where `type == "text"`).
  - `api_list_permissions(base_url: str, session_id: str) -> list[dict]` — `GET {base_url}/session/{session_id}/permission`.
  - `api_reject_permission(base_url: str, session_id: str, request_id: str) -> None` — `POST {base_url}/session/{session_id}/permission/{request_id}/reply` with `{"reply": "reject"}`.
  - `api_abort_session(base_url: str, session_id: str) -> None` — `POST {base_url}/session/{session_id}/abort`. Confirmed live: returns HTTP 200 with body `true`.
  - `find_manual_run_marker(messages: list[dict]) -> tuple[str, str] | None` — scans the most recent assistant message with `info.time.completed` set, joins its `type == "text"` parts, and applies `MARKER_RE` (ported verbatim from `rpc-bridge.py`, minus the `ASK` alternative since gnhf.py doesn't run an ask channel). Returns `(kind, detail)` where `kind` is `"DONE"` or `"BAILED"`, or `None` if no assistant message has completed yet or none contains a marker.
- Consumed by: Task 6 (launch), Task 7 (poll), Task 8 (opencode smoke-test).

- [ ] **Step 1: Write the failing tests**

```python
def test_api_create_session_posts_permission_and_returns_id(monkeypatch):
    from gnhf import api_create_session

    captured = {}

    class FakeResponse:
        status_code = 200
        def json(self):
            return {"id": "ses_abc123", "directory": "/tmp/wt"}
        def raise_for_status(self):
            pass

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setattr("requests.post", fake_post)
    ruleset = [{"permission": "edit", "pattern": "*", "action": "allow"}]
    session_id = api_create_session("http://127.0.0.1:4100", ruleset)
    assert session_id == "ses_abc123"
    assert captured["url"] == "http://127.0.0.1:4100/session"
    assert captured["json"] == {"permission": ruleset}


def test_api_prompt_async_sends_text_part(monkeypatch):
    from gnhf import api_prompt_async

    captured = {}

    class FakeResponse:
        status_code = 204
        def raise_for_status(self):
            pass

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setattr("requests.post", fake_post)
    api_prompt_async("http://127.0.0.1:4100", "ses_abc123", "do the task")
    assert captured["url"] == "http://127.0.0.1:4100/session/ses_abc123/prompt_async"
    assert captured["json"] == {"parts": [{"type": "text", "text": "do the task"}]}


ASSISTANT_DONE_MESSAGES = [
    {
        "info": {"role": "user", "time": {"created": 1}},
        "parts": [{"type": "text", "text": "do the task"}],
    },
    {
        "info": {"role": "assistant", "time": {"created": 2, "completed": 3}},
        "parts": [
            {"type": "step-start"},
            {"type": "text", "text": "Working on it...\nMANUAL_RUN: DONE — shipped the fix"},
        ],
    },
]

ASSISTANT_STILL_RUNNING_MESSAGES = [
    {
        "info": {"role": "user", "time": {"created": 1}},
        "parts": [{"type": "text", "text": "do the task"}],
    },
    {
        "info": {"role": "assistant", "time": {"created": 2}},
        "parts": [{"type": "text", "text": "still working"}],
    },
]


def test_find_manual_run_marker_detects_done():
    from gnhf import find_manual_run_marker
    result = find_manual_run_marker(ASSISTANT_DONE_MESSAGES)
    assert result == ("DONE", "shipped the fix")


def test_find_manual_run_marker_none_while_running():
    from gnhf import find_manual_run_marker
    assert find_manual_run_marker(ASSISTANT_STILL_RUNNING_MESSAGES) is None


def test_api_reject_permission_sends_reject(monkeypatch):
    from gnhf import api_reject_permission

    captured = {}

    class FakeResponse:
        status_code = 200
        def raise_for_status(self):
            pass

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setattr("requests.post", fake_post)
    api_reject_permission("http://127.0.0.1:4100", "ses_abc123", "perm_1")
    assert captured["url"] == "http://127.0.0.1:4100/session/ses_abc123/permission/perm_1/reply"
    assert captured["json"] == {"reply": "reject"}


def test_api_abort_session_posts_abort(monkeypatch):
    from gnhf import api_abort_session

    captured = {}

    class FakeResponse:
        status_code = 200
        def raise_for_status(self):
            pass

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        return FakeResponse()

    monkeypatch.setattr("requests.post", fake_post)
    api_abort_session("http://127.0.0.1:4100", "ses_abc123")
    assert captured["url"] == "http://127.0.0.1:4100/session/ses_abc123/abort"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd skills/gnhf/scripts && uv run --script test_gnhf.py -k "test_api_create_session_posts_permission_and_returns_id or test_api_prompt_async_sends_text_part or test_find_manual_run_marker or test_api_reject_permission_sends_reject or test_api_abort_session_posts_abort" -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Add the API client to `gnhf.py`** (insert after Task 4's lifecycle helper)

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd skills/gnhf/scripts && uv run --script test_gnhf.py -k "test_api_create_session_posts_permission_and_returns_id or test_api_prompt_async_sends_text_part or test_find_manual_run_marker or test_api_reject_permission_sends_reject or test_api_abort_session_posts_abort" -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add skills/gnhf/scripts/gnhf.py skills/gnhf/scripts/test_gnhf.py
git commit -m "feat(gnhf): add opencode session API client and marker detection"
```

---

## Task 6: State file read/write

**Files:**
- Modify: `skills/gnhf/scripts/gnhf.py`
- Test: `skills/gnhf/scripts/test_gnhf.py`

**Interfaces:**
- Produces: `write_state_file(path: Path, *, base_url: str, session_id: str, server_pid: int, worktree: str, launched_at: str, ttl: int) -> None` (writes JSON) and `read_state_file(path: Path) -> dict` (reads it back). Consumed by Task 7 (`--poll` mode) and Task 8 (launch orchestration writes it).

- [ ] **Step 1: Write the failing tests**

```python
def test_state_file_round_trips(tmp_path):
    from gnhf import write_state_file, read_state_file

    state_path = tmp_path / "TASK-1.state.json"
    write_state_file(
        state_path,
        base_url="http://127.0.0.1:4100",
        session_id="ses_abc123",
        server_pid=4242,
        worktree="/path/to/worktrees/TASK-1",
        launched_at="2026-09-17T08:00:00+00:00",
        ttl=10800,
    )
    state = read_state_file(state_path)
    assert state == {
        "base_url": "http://127.0.0.1:4100",
        "session_id": "ses_abc123",
        "server_pid": 4242,
        "worktree": "/path/to/worktrees/TASK-1",
        "launched_at": "2026-09-17T08:00:00+00:00",
        "ttl": 10800,
    }
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd skills/gnhf/scripts && uv run --script test_gnhf.py -k test_state_file_round_trips -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Add state file helpers to `gnhf.py`** (insert after Task 5's API client)

```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd skills/gnhf/scripts && uv run --script test_gnhf.py -k test_state_file_round_trips -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add skills/gnhf/scripts/gnhf.py skills/gnhf/scripts/test_gnhf.py
git commit -m "feat(gnhf): add opencode launch state file read/write"
```

---

## Task 7: `--poll` mode

**Files:**
- Modify: `skills/gnhf/scripts/gnhf.py`
- Test: `skills/gnhf/scripts/test_gnhf.py`

**Interfaces:**
- Consumes: `read_state_file`, `api_get_messages`, `find_manual_run_marker`, `api_list_permissions`, `api_reject_permission`, `api_abort_session` (Tasks 5-6).
- Produces: `run_poll(state_path: str) -> int` — prints exactly one status line (`RUNNING`, `MANUAL_RUN: DONE — ...`, `MANUAL_RUN: BAILED — ...`, `TTL_EXPIRED: ...`, `SERVER_DIED: ...`) and returns `EXIT_OK` in every case except `SERVER_DIED` (`EXIT_FAIL`) — the monitor step (SKILL.md step 6) reads this line the same way it reads today's `.status` file first line. Wired into `main()`'s `--poll` branch.

- [ ] **Step 1: Write the failing tests**

```python
def test_run_poll_reports_running_with_no_marker(tmp_path, monkeypatch):
    from gnhf import run_poll, write_state_file

    state_path = tmp_path / "s.json"
    write_state_file(
        state_path, base_url="http://127.0.0.1:4100", session_id="ses1",
        server_pid=99999999, worktree=str(tmp_path), launched_at="2026-09-17T08:00:00+00:00", ttl=10800,
    )
    monkeypatch.setattr("gnhf.api_get_messages", lambda *a, **k: [])
    monkeypatch.setattr("gnhf.api_list_permissions", lambda *a, **k: [])
    monkeypatch.setattr("gnhf.process_alive", lambda pid: True)

    rc, output = _capture(lambda: run_poll(str(state_path)))
    assert rc == 0
    assert output.strip() == "RUNNING"


def test_run_poll_reports_done_marker(tmp_path, monkeypatch):
    from gnhf import run_poll, write_state_file

    state_path = tmp_path / "s.json"
    write_state_file(
        state_path, base_url="http://127.0.0.1:4100", session_id="ses1",
        server_pid=99999999, worktree=str(tmp_path), launched_at="2026-09-17T08:00:00+00:00", ttl=10800,
    )
    monkeypatch.setattr("gnhf.api_get_messages", lambda *a, **k: ASSISTANT_DONE_MESSAGES)
    monkeypatch.setattr("gnhf.api_list_permissions", lambda *a, **k: [])
    monkeypatch.setattr("gnhf.process_alive", lambda pid: True)

    rc, output = _capture(lambda: run_poll(str(state_path)))
    assert rc == 0
    assert "MANUAL_RUN: DONE" in output
    assert "shipped the fix" in output


def test_run_poll_rejects_out_of_policy_permission(tmp_path, monkeypatch):
    from gnhf import run_poll, write_state_file

    state_path = tmp_path / "s.json"
    write_state_file(
        state_path, base_url="http://127.0.0.1:4100", session_id="ses1",
        server_pid=99999999, worktree=str(tmp_path), launched_at="2026-09-17T08:00:00+00:00", ttl=10800,
    )
    monkeypatch.setattr("gnhf.api_get_messages", lambda *a, **k: [])
    monkeypatch.setattr("gnhf.api_list_permissions", lambda *a, **k: [{"id": "perm_1"}])
    rejected = []
    monkeypatch.setattr("gnhf.api_reject_permission", lambda base, sid, rid: rejected.append(rid))
    monkeypatch.setattr("gnhf.process_alive", lambda pid: True)

    _capture(lambda: run_poll(str(state_path)))
    assert rejected == ["perm_1"]


def test_run_poll_aborts_on_ttl_expiry(tmp_path, monkeypatch):
    from gnhf import run_poll, write_state_file

    state_path = tmp_path / "s.json"
    write_state_file(
        state_path, base_url="http://127.0.0.1:4100", session_id="ses1",
        server_pid=99999999, worktree=str(tmp_path),
        launched_at="2020-01-01T00:00:00+00:00",  # far in the past -- always expired
        ttl=1,
    )
    aborted = []
    monkeypatch.setattr("gnhf.api_get_messages", lambda *a, **k: [])
    monkeypatch.setattr("gnhf.api_list_permissions", lambda *a, **k: [])
    monkeypatch.setattr("gnhf.api_abort_session", lambda base, sid: aborted.append(sid))
    monkeypatch.setattr("gnhf.process_alive", lambda pid: True)

    rc, output = _capture(lambda: run_poll(str(state_path)))
    assert rc == 0
    assert output.startswith("TTL_EXPIRED")
    assert aborted == ["ses1"]


def test_run_poll_reports_server_died(tmp_path, monkeypatch):
    from gnhf import run_poll, write_state_file

    state_path = tmp_path / "s.json"
    write_state_file(
        state_path, base_url="http://127.0.0.1:4100", session_id="ses1",
        server_pid=99999999, worktree=str(tmp_path), launched_at="2026-09-17T08:00:00+00:00", ttl=10800,
    )
    monkeypatch.setattr("gnhf.process_alive", lambda pid: False)

    rc, output = _capture(lambda: run_poll(str(state_path)))
    assert rc == 1
    assert output.startswith("SERVER_DIED")
```

Add this shared helper near the top of `test_gnhf.py`, alongside the other imports:

```python
import contextlib
import io


def _capture(fn):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn()
    return rc, buf.getvalue()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd skills/gnhf/scripts && uv run --script test_gnhf.py -k "test_run_poll" -v`
Expected: FAIL — `ImportError: cannot import name 'run_poll'`

- [ ] **Step 3: Add `run_poll` and `process_alive` to `gnhf.py`** (insert after Task 6's state file helpers)

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd skills/gnhf/scripts && uv run --script test_gnhf.py -k "test_run_poll" -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add skills/gnhf/scripts/gnhf.py skills/gnhf/scripts/test_gnhf.py
git commit -m "feat(gnhf): add --poll mode for opencode launches"
```

---

## Task 8: `-l --agent opencode` launch orchestration (with 429 probe/backoff and optional Herdr pane)

**Files:**
- Modify: `skills/gnhf/scripts/gnhf.py`
- Test: `skills/gnhf/scripts/test_gnhf.py`

**Interfaces:**
- Consumes: `start_opencode_serve`, `api_create_session`, `api_prompt_async`, `build_gnhf_permission_ruleset`, `write_state_file`, `is_rate_limited`, `backoff_delay` (Tasks 2-6).
- Produces: `run_launch_opencode(cwd, log_path, ttl, probe, base_backoff, max_backoff, max_429, total_backoff_cap, prompt, herdr_pane) -> int`. Wired into `main()`'s `-l --agent opencode` branch. Prints `LAUNCHED: session=<id> port=<port> pid=<server_pid> state=<state_path> ttl_expires=<iso>` on success.

For the probe window: after `api_create_session` + `api_prompt_async`, poll `api_get_messages` for `probe` seconds watching for a rate-limited-looking string anywhere in a message part's text (mirrors upstream's `attempt_output()` scan, but against API responses instead of a log file) or the server process dying. This reuses the exact same `is_rate_limited`/`backoff_delay`/retry-ceiling contract as upstream and as Task 7's sibling `pi` path (Task 9).

- [ ] **Step 1: Write the failing tests**

```python
def test_run_launch_opencode_happy_path(tmp_path, monkeypatch):
    from gnhf import run_launch_opencode, read_state_file

    monkeypatch.setattr("gnhf.start_opencode_serve", lambda cwd, log: (_FakeProc(pid=555), 4100))
    monkeypatch.setattr("gnhf.api_create_session", lambda base_url, permission, directory=None: "ses_new")
    sent = []
    monkeypatch.setattr("gnhf.api_prompt_async", lambda base_url, sid, text: sent.append((sid, text)))
    monkeypatch.setattr("gnhf.api_get_messages", lambda *a, **k: [])  # no rate-limit text during probe
    monkeypatch.setattr("gnhf.maybe_open_herdr_pane", lambda *a, **k: None)
    monkeypatch.setattr("time.sleep", lambda s: None)

    log_path = tmp_path / "TASK-1.log"
    rc, output = _capture(lambda: run_launch_opencode(
        cwd=str(tmp_path), log_path=str(log_path), ttl=10800, probe=0.01,
        base_backoff=1, max_backoff=2, max_429=2, total_backoff_cap=10,
        prompt="do the task", herdr_pane=False,
    ))
    assert rc == 0
    assert "LAUNCHED: session=ses_new" in output
    assert sent == [("ses_new", "do the task")]
    state = read_state_file(tmp_path / "TASK-1.state.json")
    assert state["session_id"] == "ses_new"
    assert state["base_url"] == "http://127.0.0.1:4100"


def test_run_launch_opencode_early_exit_when_server_dies_in_probe(tmp_path, monkeypatch):
    from gnhf import run_launch_opencode, EXIT_EARLY_EXIT

    dead_proc = _FakeProc(pid=555, dead=True)
    monkeypatch.setattr("gnhf.start_opencode_serve", lambda cwd, log: (dead_proc, 4100))
    monkeypatch.setattr("gnhf.api_create_session", lambda *a, **k: (_ for _ in ()).throw(
        __import__("requests").exceptions.ConnectionError("refused")))
    monkeypatch.setattr("gnhf.maybe_open_herdr_pane", lambda *a, **k: None)
    monkeypatch.setattr("time.sleep", lambda s: None)

    log_path = tmp_path / "TASK-2.log"
    rc, output = _capture(lambda: run_launch_opencode(
        cwd=str(tmp_path), log_path=str(log_path), ttl=10800, probe=0.01,
        base_backoff=1, max_backoff=2, max_429=2, total_backoff_cap=10,
        prompt="do the task", herdr_pane=False,
    ))
    assert rc == EXIT_EARLY_EXIT
    assert "EARLY_EXIT" in output


class _FakeProc:
    def __init__(self, pid, dead=False):
        self.pid = pid
        self._dead = dead
        self.returncode = 1 if dead else None

    def poll(self):
        return self.returncode

    def terminate(self):
        pass

    def wait(self, timeout=None):
        pass
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd skills/gnhf/scripts && uv run --script test_gnhf.py -k "test_run_launch_opencode" -v`
Expected: FAIL — `ImportError: cannot import name 'run_launch_opencode'`

- [ ] **Step 3: Add `run_launch_opencode` and `maybe_open_herdr_pane` to `gnhf.py`** (insert after Task 7's `run_poll`)

```python
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
```

(Note: `herdr_pane=None`, the CLI default from Task 1's `--herdr-pane`/`--no-herdr-pane` flags, should be resolved to "on iff `HERDR_ENV=1`" in `main()` before calling `run_launch_opencode` — `maybe_open_herdr_pane` itself also checks `HERDR_ENV`, so this is belt-and-suspenders, not a second required check.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd skills/gnhf/scripts && uv run --script test_gnhf.py -k "test_run_launch_opencode" -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add skills/gnhf/scripts/gnhf.py skills/gnhf/scripts/test_gnhf.py
git commit -m "feat(gnhf): launch opencode via serve+session API with 429 probe/backoff"
```

---

## Task 9: `-l --agent pi` (ported unchanged) and `-s` smoke-test modes, wire up `main()`

**Files:**
- Modify: `skills/gnhf/scripts/gnhf.py`
- Test: `skills/gnhf/scripts/test_gnhf.py`

**Interfaces:**
- Consumes: everything from Tasks 1-8.
- Produces: `run_launch_pi(cwd, log_path, ttl, probe, base_backoff, max_backoff, max_429, total_backoff_cap, command) -> int` (upstream's `run_launch`, renamed, unchanged behavior — subprocess+`timeout`+probe+backoff around an arbitrary command), `build_smoke_cmd(agent, provider, model, path) -> tuple[list[str]|None, str|None]` (upstream-identical), `run_smoke_test(agent, provider, model, path, timeout_s, max_retries, base_backoff, max_backoff) -> int` for `pi`, and a new `run_smoke_test_opencode(provider, model, timeout_s, max_retries) -> int` that starts a throwaway `opencode serve` in a temp directory, creates a session, sends `SMOKE_TEST_PROMPT` via `api_prompt_async`, polls `api_get_messages` up to `timeout_s` for a completed assistant reply containing "PONG", tears the server down, and reports `PASS:`/`FAIL:`/`RATE_LIMITED:` exactly like the `pi` path. `main()` dispatches all three CLI modes (`-s`, `-l`, `--poll`) to these functions.

- [ ] **Step 1: Write the failing tests**

```python
def test_build_smoke_cmd_pi_missing_binary(monkeypatch):
    from gnhf import build_smoke_cmd
    monkeypatch.setattr("shutil.which", lambda name: None)
    cmd, err = build_smoke_cmd("pi", None, None, "/tmp")
    assert cmd is None
    assert "not on PATH" in err


def test_smoke_test_opencode_reports_pass(tmp_path, monkeypatch):
    from gnhf import run_smoke_test_opencode

    monkeypatch.setattr("gnhf.start_opencode_serve", lambda cwd, log: (_FakeProc(pid=1), 4100))
    monkeypatch.setattr("gnhf.api_create_session", lambda *a, **k: "ses_smoke")
    monkeypatch.setattr("gnhf.api_prompt_async", lambda *a, **k: None)
    monkeypatch.setattr("gnhf.api_get_messages", lambda *a, **k: [
        {"info": {"role": "assistant", "time": {"created": 1, "completed": 2}},
         "parts": [{"type": "text", "text": "PONG"}]},
    ])
    monkeypatch.setattr("time.sleep", lambda s: None)

    rc, output = _capture(lambda: run_smoke_test_opencode(
        provider=None, model=None, timeout_s=5, max_retries=1,
    ))
    assert rc == 0
    assert "PASS:" in output


def test_smoke_test_opencode_reports_fail_on_wrong_reply(tmp_path, monkeypatch):
    from gnhf import run_smoke_test_opencode

    monkeypatch.setattr("gnhf.start_opencode_serve", lambda cwd, log: (_FakeProc(pid=1), 4100))
    monkeypatch.setattr("gnhf.api_create_session", lambda *a, **k: "ses_smoke")
    monkeypatch.setattr("gnhf.api_prompt_async", lambda *a, **k: None)
    monkeypatch.setattr("gnhf.api_get_messages", lambda *a, **k: [
        {"info": {"role": "assistant", "time": {"created": 1, "completed": 2}},
         "parts": [{"type": "text", "text": "something else entirely"}]},
    ])
    monkeypatch.setattr("time.sleep", lambda s: None)

    rc, output = _capture(lambda: run_smoke_test_opencode(
        provider=None, model=None, timeout_s=5, max_retries=1,
    ))
    assert rc == 1
    assert "FAIL:" in output
```

`build_smoke_cmd` (pi branch), `run_launch_pi`, and `run_smoke_test` (pi
branch) are ported byte-for-byte from upstream's `gnhf.py` (already
covered by upstream's own `test_gnhf.py`, which Task 10 ports separately)
— no new tests needed for those beyond `test_build_smoke_cmd_pi_missing_binary`
above confirming the port landed correctly.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd skills/gnhf/scripts && uv run --script test_gnhf.py -k "test_build_smoke_cmd_pi_missing_binary or test_smoke_test_opencode" -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Add the remaining functions and wire up `main()`**

Add after Task 8's `run_launch_opencode`:

```python
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
```

Add `run_launch_pi` (upstream's `run_launch`, renamed) right before `build_smoke_cmd`:

```python
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
```

- [ ] **Step 4: Run the full test suite to verify everything passes**

Run: `cd skills/gnhf/scripts && uv run --script test_gnhf.py -v`
Expected: PASS (all tests from Tasks 1-9)

- [ ] **Step 5: Commit**

```bash
git add skills/gnhf/scripts/gnhf.py skills/gnhf/scripts/test_gnhf.py
git commit -m "feat(gnhf): port pi launch/smoke-test paths, add opencode smoke-test, wire up main()"
```

---

## Task 10: Remove superseded scripts and reshape MCP defaults

**Files:**
- Delete: `skills/gnhf/scripts/rpc-bridge.py`
- Delete: `skills/gnhf/scripts/rpc-bridge-smoke-test.sh`
- Delete: `skills/gnhf/scripts/smoke-test.sh`
- Delete: `skills/gnhf/scripts/pi-extensions/plan-mode/index.ts`
- Delete: `skills/gnhf/scripts/pi-extensions/plan-mode/utils.ts`
- Delete: `skills/gnhf/scripts/pi-extensions/plan-mode/README.md`
- Modify: `skills/gnhf/scripts/mcp-defaults.json`

- [ ] **Step 1: Delete the superseded files**

```bash
git rm skills/gnhf/scripts/rpc-bridge.py
git rm skills/gnhf/scripts/rpc-bridge-smoke-test.sh
git rm skills/gnhf/scripts/smoke-test.sh
git rm -r skills/gnhf/scripts/pi-extensions
```

- [ ] **Step 2: Reshape `mcp-defaults.json`** — from pi's `mcpServers` object (consumed by `pi --mcp-config`) to a list of `POST /mcp` request bodies (`{"name": ..., "config": {...}}`, `McpLocalConfig` requires `type` and `command`; confirmed live against `opencode serve`'s `/doc`):

```json
[
  {
    "name": "context7",
    "config": {
      "type": "local",
      "command": ["npx", "-y", "@upstash/context7-mcp"],
      "enabled": true
    }
  },
  {
    "name": "godot",
    "config": {
      "type": "local",
      "command": ["./tools/run.py", "godot-mcp"],
      "environment": { "DISPLAY": ":99" },
      "enabled": true
    }
  }
]
```

This file isn't wired into `gnhf.py` in this plan (no task above reads it)
— it's left as a template for whichever repo-specific launch wrapper wants
to `POST /mcp` these after `start_opencode_serve` returns, matching how
the original `mcp-defaults.json` was itself repo-specific, opt-in config
rather than something `gnhf.py` read directly.

- [ ] **Step 3: Verify nothing else references the deleted files**

Run: `cd /home/alex/git/ai_skills && grep -rn "rpc-bridge\|plan-mode" skills/gnhf/ --include="*.md" --include="*.py"`
Expected: no matches outside `SKILL.md` and `README.md` (those are rewritten in Tasks 11-12, not yet reached — matches there are expected and will be cleaned up next).

- [ ] **Step 4: Commit**

```bash
git add skills/gnhf/scripts/mcp-defaults.json
git commit -m "chore(gnhf): remove rpc-bridge.py and plan-mode extension, reshape mcp-defaults.json

Superseded by gnhf.py's opencode server API integration: opencode's
native session-scoped permission ruleset (Task 3) replaces plan-mode's
regex-based command scanning outright, and the server's own session API
replaces rpc-bridge's bespoke pi-only ASK channel."
```

---

## Task 11: Rewrite `SKILL.md` steps 1, 5, 6, 7, and "Bundled scripts"

**Files:**
- Modify: `skills/gnhf/SKILL.md`

- [ ] **Step 1: Update step 1's agent tie-break default**

Find the line in step 1's numbered list:

```
1. `which pi opencode claude codex copilot 2>/dev/null` — which agent CLIs
   are actually on `PATH` right now. If none are found, stop here and
   report that no supported agent CLI is installed; don't guess or try to
   install one. Default to `pi` if more than one is found and nothing else
   disambiguates (matches this user's usual setup).
```

Replace `Default to `pi` if more than one is found` with:

```
1. `which pi opencode claude codex copilot 2>/dev/null` — which agent CLIs
   are actually on `PATH` right now. If none are found, stop here and
   report that no supported agent CLI is installed; don't guess or try to
   install one. Default to `opencode` if more than one is found and
   nothing else disambiguates (matches this user's usual setup — opencode
   runs as a genuinely interactive TUI, attachable live via a Herdr pane,
   see step 5).
```

Also replace the smoke-test invocation examples (`"${SKILL_DIR}/scripts/smoke-test.sh" pi` / `opencode`) with:

```bash
"${SKILL_DIR}/scripts/gnhf.py" -s pi
# or:
"${SKILL_DIR}/scripts/gnhf.py" -s opencode
```

- [ ] **Step 2: Replace step 5 ("Launch, bounded") entirely**

Replace the whole step 5 section (from `## 5. Launch, bounded` through
just before `## 6. Monitor with minimal oversight`) with:

```markdown
## 5. Launch, bounded

The wall-clock bound (TTL) is the primary and simplest bound.

**If the resolved agent is `opencode`** (the default): `gnhf.py -l --agent
opencode` starts `opencode serve` detached in the worktree, creates a
session with gnhf's own permission ruleset (allow edit/webfetch/bash,
explicit deny list for destructive bash patterns — no `ask` entries, since
nothing will ever answer one unattended), and sends the task prompt via
the session's async prompt endpoint. Write the prompt to
`<worktree>/.gnhf-prompt.md` first — `gnhf.py` reads it from there:

```bash
cat > /path/to/worktrees/<TASK-ID>/.gnhf-prompt.md <<'PROMPT_EOF'
<full prompt from step 4>
PROMPT_EOF

"${SKILL_DIR}/scripts/gnhf.py" -l --agent opencode \
    -C /path/to/worktrees/<TASK-ID> \
    -o /path/to/logs/<TASK-ID>.log \
    -T <TTL_SECONDS>
```

If `HERDR_ENV=1`, this also opens a Herdr pane running `opencode attach
<url>` against the new session — a genuinely interactive TUI you can watch
or type into at any time. This is a visibility convenience, not the
control path: `gnhf.py` itself talks to the session over the server's REST
API the whole time, whether or not a pane exists (`--no-herdr-pane` skips
it explicitly; a failed pane creation degrades to no-pane, never fails the
launch).

**If the resolved agent is `pi`**: unchanged from before, just invoked
through the same script instead of a separate one:

```bash
"${SKILL_DIR}/scripts/gnhf.py" -l --agent pi \
    -C /path/to/worktrees/<TASK-ID> \
    -o /path/to/logs/<TASK-ID>.log \
    -T <TTL_SECONDS> \
    -- pi --session-id <task-id>-run -p "@/path/to/prompt.md"
```

Default `TTL_SECONDS` to 10800 (3h) unless the user gives a different
budget.

`gnhf.py -l` exits with one of:

| Exit | Meaning | What to do |
| --- | --- | --- |
| 0 | `LAUNCHED: ...` | move to step 6, monitor via the printed session/PID |
| 3 | `RATE_LIMITED:` — 429s past the retry ceiling | see below — never call this BAILED |
| 4 | `EARLY_EXIT:` — died inside the probe window, not a 429 | a real dispatch failure (bad flag, missing binary, unreachable server) — report it, don't retry it |

Exit 3 and exit 4 mean the task never got a turn. Neither is a `BAILED`
verdict, and neither is a reason to consider the task attempted. Only a
launch that survives the probe window (exit 0) and later produces a
`MANUAL_RUN: DONE —` / `MANUAL_RUN: BAILED —` marker, a TTL expiry, or a
thrashing kill counts as a real outcome.

A turn/iteration bound (`max-turns`) only applies when the agent is
literally invoked once per turn with session continuation — most agent
CLIs, and opencode's session API used here, run their own internal
multi-step tool-call loop, so the TTL alone already bounds those. Only
build an external per-turn loop-and-check wrapper if the user explicitly
wants turn-level granularity.
```

- [ ] **Step 3: Replace step 6 ("Monitor with minimal oversight")**

Replace the whole step 6 section with:

```markdown
## 6. Monitor with minimal oversight

The TTL clock starts at the `LAUNCHED:` line from step 5, not at the first
launch attempt — any attempts killed for rate-limiting don't count against
the budget.

**If the resolved agent is `opencode`**: each `ScheduleWakeup` tick, run
one non-blocking poll against the state file `gnhf.py -l` printed:

```bash
"${SKILL_DIR}/scripts/gnhf.py" --poll /path/to/logs/<TASK-ID>.state.json
```

This prints exactly one line:

- `RUNNING` — no terminal marker yet; keep waiting. Any pending permission
  request outside gnhf's ruleset was already auto-rejected as a side
  effect of this same poll — that's the safety net, not a sign of trouble
  unless it keeps recurring for the same command (a thrashing signal, see
  below).
- `MANUAL_RUN: DONE — ...` / `MANUAL_RUN: BAILED — ...` — proceed to step
  7 exactly as before.
- `TTL_EXPIRED: ...` — the session was just cleanly aborted; proceed to
  step 7's "don't push anything" branch.
- `SERVER_DIED: ...` — `opencode serve` itself is no longer running (not a
  clean abort). Treat like a silent marker-less exit: read the log at
  `/path/to/logs/<TASK-ID>.log` for what happened, report it as the
  blocker, don't relaunch on a guess.

If a Herdr pane is running the TUI, you can also glance at it directly
(`herdr agent read <pane_id> --source recent-unwrapped --lines 120`) for a
human-readable view of the same session `--poll` is checking
structurally — useful for judging genuine progress vs. thrashing, since
`--poll`'s `RUNNING` alone doesn't distinguish the two.

**If the resolved agent is `pi`**: unchanged —

```bash
tail -n 40 /path/to/logs/<TASK-ID>.log
ps -p <PID> -o pid,etime,stat
cd /path/to/worktrees/<TASK-ID> && git log --oneline -5 && git status --short
```

Silent exit (process gone, no `MANUAL_RUN` marker in the log) is treated
exactly as before: a `BAILED`-equivalent, report the blocker from the
log's last ~50 lines, don't relaunch on a guess.

**For both agents**: only intervene (nudge, or kill) on genuine thrashing
signals — the same failing command repeating verbatim, no commits after a
long stretch with the identical error recurring, or an obvious loop. A run
that's slow but making incremental progress is not thrashing — let it
continue. Otherwise let it run until a terminal `--poll` line / log marker
appears, the TTL expires, or you've confirmed real thrashing.
```

- [ ] **Step 4: Update step 7's opening paragraph only**

Find:

```
On `MANUAL_RUN: BAILED —`, a silent marker-less exit (non-pi agents; step
6), a `PROCESS_EXITED`/`BRIDGE_ERROR` status (pi via `rpc-bridge.py`; step
6), TTL expiry, or a thrashing kill: don't push anything.
```

Replace with:

```
On `MANUAL_RUN: BAILED —`, a silent marker-less exit (`pi`; step 6), a
`SERVER_DIED` poll result (`opencode`; step 6), TTL expiry, or a thrashing
kill: don't push anything.
```

The rest of step 7 (the diff/AC review checklist, the merge authorization,
the Monitor-stop reminder) is unchanged — none of it is specific to which
agent produced the work.

- [ ] **Step 5: Replace the "Bundled scripts" section**

Replace the whole section (from `## Bundled scripts` to the end of the
file) with:

```markdown
## Bundled scripts

`scripts/gnhf.py` — a self-contained `uv run --script` (PEP 723) tool with
three modes:

- `-s`/`--smoke-test <pi|opencode>` — verifies the agent can reach its
  currently configured model and produce a real response (see step 1),
  retrying transient 429s on its own backoff before reporting `FAIL:` or
  `RATE_LIMITED:`. For `opencode`, this drives the same server-API path
  real launches use (`opencode serve` → create session → send prompt →
  poll for the reply), not `opencode run`.
- `-l`/`--launch --agent <pi|opencode>` — launches the real task run (see
  step 5). For `pi`, unchanged subprocess+`timeout`+probe+backoff around
  the given command. For `opencode`, starts `opencode serve`, creates a
  session with gnhf's permission ruleset, sends the prompt, and writes a
  state file for `--poll` to read — no subprocess/`timeout` wrapping,
  since there's no single foreground process to bound; TTL enforcement
  happens in `--poll` via a clean session abort instead.
- `--poll <state-file>` — one non-blocking check of an `opencode` launch
  (see step 6): scans for a `MANUAL_RUN` marker, auto-rejects any pending
  permission request outside the ruleset, aborts the session if the TTL
  has elapsed.

Every tunable (TTL, probe window, backoff base/cap, retry ceilings)
resolves through `python-decouple`: CLI flag > process env >
`skills/gnhf/.env` > hardcoded default. See `.env.example` for the full
list of `GNHF_*` names — copy it to `.env` in this same directory to
override the defaults for this machine. Run `scripts/gnhf.py -h` for the
full flag list, and review the script before first use to verify
behavior.

`scripts/test_gnhf.py` — the accompanying pytest suite, also a
self-contained `uv run --script`. All HTTP calls are mocked; no live
`opencode serve` process is required to run it. Run it directly
(`./scripts/test_gnhf.py`) after changing `gnhf.py`.

**Superseded**: `rpc-bridge.py`, `rpc-bridge-smoke-test.sh`, and the
vendored `pi-extensions/plan-mode/` extension (nine documented
false-positive fixes to a regex-based command scanner) are gone.
opencode's session-scoped permission ruleset (structured `{permission,
pattern, action}` objects the server evaluates itself) replaces
plan-mode's job with something that can't have the same class of bug —
it's never matching against a raw shell string in the first place. The
server's own multi-client session model (the TUI and `gnhf.py` are both
just HTTP clients of the same session) replaces rpc-bridge's bespoke
pi-only live-ASK channel.
```

- [ ] **Step 6: Commit**

```bash
git add skills/gnhf/SKILL.md
git commit -m "docs(gnhf): rewrite SKILL.md launch/monitor/finish for opencode server API"
```

---

## Task 12: Rewrite `skills/gnhf/README.md` and repo-root `README.md`

**Files:**
- Modify: `skills/gnhf/README.md`
- Modify: `README.md` (repo root)

- [ ] **Step 1: Update `skills/gnhf/README.md`'s Quickstart table and Parameters**

Replace the Quickstart table:

```markdown
| Agent | Command |
| ----- | ------- |
| Claude Code | `/gnhf <task-id-or-description> [ttl] [max-turns]` |
| pi | `/skill:gnhf <task-id-or-description> [ttl] [max-turns]` |
| opencode | describe the task in chat; opencode loads the skill from its description |
```

with:

```markdown
| Agent | Command |
| ----- | ------- |
| Claude Code | `/gnhf <task-id-or-description> [ttl] [max-turns]` |
| opencode | describe the task in chat; opencode loads the skill from its description — this is the default unattended agent, driven through opencode's own server API with a live TUI attached in a Herdr pane when available |
| pi | `/skill:gnhf <task-id-or-description> [ttl] [max-turns]` — still supported as a secondary agent |
```

Add a new section after "## Parameters", before "## Example":

```markdown
## Configuration

`scripts/gnhf.py`'s defaults can be overridden per machine without editing
the script: set the env var directly, or copy `.env.example` to `.env` in
this same directory (resolved relative to the script, not the caller's
cwd; gitignored). A CLI flag always wins over either.

| Env var | Default | Mode |
| --- | --- | --- |
| `GNHF_PROVIDER` | unset | smoke-test |
| `GNHF_MODEL` | unset | smoke-test |
| `GNHF_TIMEOUT` | `300` | smoke-test |
| `GNHF_MAX_RETRIES` | `2` | smoke-test |
| `GNHF_TTL` | `10800` | launch |
| `GNHF_PROBE` | `25` | launch |
| `GNHF_BASE_BACKOFF` | `75` | launch |
| `GNHF_MAX_BACKOFF` | `900` | launch |
| `GNHF_MAX_429` | `6` | launch |
| `GNHF_TOTAL_BACKOFF_CAP` | `2700` | launch |
```

Replace "## What happens after"'s closing sentence:

```
The skill reports back one of: `DONE` (reviewed, pushed, and merged),
`BAILED` (blocked, nothing pushed), TTL-expired, or killed for thrashing —
along with the worktree and log paths for manual review. See
[SKILL.md](SKILL.md) steps 6-7 for the monitoring and finish protocol.
```

with:

```
The skill reports back one of: `DONE` (reviewed, pushed, and merged),
`BAILED` (blocked, nothing pushed), `RATE_LIMITED` or `EARLY_EXIT` (the
task never got a turn — a launch that died on gateway contention or a real
dispatch failure, not an outcome of the task itself), TTL-expired, or
killed for thrashing — along with the worktree and log paths for manual
review. If the agent is `opencode`, a Herdr pane running the live TUI
stays open for you to inspect the finished session directly. See
[SKILL.md](SKILL.md) steps 5-7 for the launch, monitoring, and finish
protocol.
```

- [ ] **Step 2: Update the repo-root `README.md`**

Find and remove this paragraph from the top intro:

```
On top of upstream, the `gnhf-pi-rpc-bridge` branch adds a live ASK channel
and plan-mode auto-execute for `gnhf`'s unattended `pi` runs — see
[skills/gnhf/scripts/rpc-bridge.py](skills/gnhf/scripts/rpc-bridge.py) and
[skills/gnhf/scripts/pi-extensions/plan-mode/](skills/gnhf/scripts/pi-extensions/plan-mode/).
```

Replace with:

```
On top of upstream, `gnhf` drives its unattended runs through opencode's
own server API by default — a real interactive TUI stays attached and
watchable (in a Herdr pane, when available) while a session-scoped
permission ruleset and polling loop handle everything unattended.
`pi` remains supported as a secondary agent. See
[skills/gnhf/scripts/gnhf.py](skills/gnhf/scripts/gnhf.py) and
[skills/gnhf/SKILL.md](skills/gnhf/SKILL.md).
```

Find and remove the whole "install from the `gnhf-pi-rpc-bridge` branch"
block:

```
The `gnhf-pi-rpc-bridge` branch (pi live-ASK channel + plan-mode
auto-execute) isn't merged to `main` yet. Until it is, install straight
from that branch with the CLI's direct-path form instead:

```bash
npx skills add https://github.com/zookinheimer/ai_skills/tree/gnhf-pi-rpc-bridge/skills/gnhf
```

```

Find the "Manual install" section's branch-checkout line:

```bash
git clone https://github.com/zookinheimer/ai_skills.git ~/git/ai_skills
cd ~/git/ai_skills && git checkout gnhf-pi-rpc-bridge   # until it's merged to main
```

Replace with:

```bash
git clone https://github.com/zookinheimer/ai_skills.git ~/git/ai_skills
```

- [ ] **Step 3: Verify no other stale references remain**

Run: `cd /home/alex/git/ai_skills && grep -rn "gnhf-pi-rpc-bridge\|rpc-bridge\|plan-mode" README.md skills/gnhf/README.md`
Expected: no matches.

- [ ] **Step 4: Commit**

```bash
git add README.md skills/gnhf/README.md
git commit -m "docs(gnhf): update READMEs for opencode-TUI default, drop stale branch install instructions"
```

---

## Task 13: Manual pre-merge smoke test against a live `opencode serve`

This is a manual checklist, not automated — it needs a real model backend,
matching the spec's testing section.

- [ ] **Step 1: Run the automated suite one more time end to end**

Run: `cd skills/gnhf/scripts && uv run --script test_gnhf.py -v`
Expected: all tests pass.

- [ ] **Step 2: Live smoke-test both agents**

```bash
./gnhf.py -s opencode
./gnhf.py -s pi
```

Expected: both print `PASS: ... responded correctly in ...s` (or a clear
`FAIL:`/`RATE_LIMITED:` diagnosis if the local model backend isn't up —
not itself a bug in this plan's code).

- [ ] **Step 3: Live end-to-end launch against a throwaway task**

Pick or write a trivial, well-specced test task (e.g. "add a one-line
comment to `skills/gnhf/README.md` explaining what `.env.example` is
for") in a scratch worktree, then:

```bash
mkdir -p /tmp/gnhf-e2e-worktree
cd /home/alex/git/ai_skills && git worktree add /tmp/gnhf-e2e-worktree -b gnhf-e2e-smoke
cat > /tmp/gnhf-e2e-worktree/.gnhf-prompt.md <<'EOF'
<test prompt with the trivial task + gnhf's standard HARD RULES / STUCK POLICY / FINISH PROTOCOL from SKILL.md step 4>
EOF
./gnhf.py -l --agent opencode -C /tmp/gnhf-e2e-worktree -o /tmp/gnhf-e2e.log -T 600
```

Expected: `LAUNCHED: session=... port=... pid=... state=... ttl_expires=...`.
If `HERDR_ENV=1`, confirm a Herdr pane opened showing the live TUI.

- [ ] **Step 4: Poll until terminal, confirm a deliberately out-of-policy command gets rejected**

```bash
watch -n 15 "./gnhf.py --poll /tmp/gnhf-e2e.log.state.json"
```

Confirm it eventually prints `MANUAL_RUN: DONE — ...`. If the task's own
work never happens to trigger an out-of-policy permission request
naturally, separately verify the reject path by sending a session a
prompt that asks it to run something on the deny list (e.g. `git checkout
main`) and confirming `GET {base_url}/session/{id}/permission` shows a
rejected entry after the next poll.

- [ ] **Step 5: Clean up the scratch worktree**

```bash
cd /home/alex/git/ai_skills && git worktree remove /tmp/gnhf-e2e-worktree --force
git branch -D gnhf-e2e-smoke
rm -f /tmp/gnhf-e2e.log /tmp/gnhf-e2e.log.state.json
```

- [ ] **Step 6: Push the branch and open a PR**

```bash
git push -u origin feat/gnhf-opencode-tui
gh pr create --title "gnhf: drive opencode's TUI via its server API, default off pi" --body "$(cat <<'EOF'
## Summary
- Replaces rpc-bridge.py + the vendored plan-mode extension with a single
  scripts/gnhf.py that drives opencode through its server REST API
  (session-scoped permissions, TTL via clean session abort).
- opencode + a Herdr-attached live TUI is now the default agent; pi
  remains supported as a secondary smoke-test/launch path, unchanged.
- See docs/superpowers/specs/2026-09-17-gnhf-opencode-tui-design.md and
  docs/superpowers/plans/2026-09-17-gnhf-opencode-tui.md for the full
  design and task breakdown.

## Test plan
- [x] scripts/test_gnhf.py passes (all HTTP mocked)
- [x] Live smoke test: both `gnhf.py -s opencode` and `gnhf.py -s pi`
- [x] Live end-to-end launch against a scratch worktree, polled to a DONE
      marker, out-of-policy permission reject confirmed

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Do not merge automatically — this PR is to the user's own fork's `main`,
not a throwaway task branch, and warrants the user's own review before
merging (unlike gnhf's own task-run PRs, which the skill is
pre-authorized to merge on the user's behalf).

---

## Self-Review Notes

- **Spec coverage**: every spec component (gnhf.py rewrite, permission
  ruleset, agent resolution tie-break, deprecated-file removal,
  mcp-defaults reshape, SKILL.md/README rewrites, testing) has a task.
  Both "open items" the spec flagged were resolved live before this plan
  was written (prompt endpoint = `prompt_async`, no `directory` param
  needed) rather than left as an implementation-time unknown.
- **Placeholder scan**: no TBD/TODO; every code block is complete,
  runnable Python/bash/markdown, not a description of what to write.
- **Type consistency**: `find_manual_run_marker` returns `tuple[str, str]
  | None` consistently across Task 5 (definition) and Task 7
  (`run_poll`'s usage). `api_create_session`'s `permission` parameter name
  matches `build_gnhf_permission_ruleset`'s return type (`list[dict]`)
  used identically in Task 8. State file field names (`base_url`,
  `session_id`, `server_pid`, `worktree`, `launched_at`, `ttl`) are
  identical across Task 6 (write/read), Task 7 (`run_poll` consumes them),
  and Task 8 (`run_launch_opencode` writes them).
