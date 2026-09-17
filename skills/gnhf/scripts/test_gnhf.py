#!/usr/bin/env -S uv run --script

# /// script
# requires-python = ">=3.13,<3.14"
# dependencies = [
#     "pytest>=7.0",
#     "python-decouple>=3.8",
#     "requests>=2.32",
# ]
# [tool.uv]
# exclude-newer = "2026-10-01T00:00:00Z"
# ///

import contextlib
import io
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


def _capture(fn):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = fn()
    return rc, buf.getvalue()

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


def test_run_poll_reports_running_with_no_marker(tmp_path, monkeypatch):
    from gnhf import run_poll, write_state_file

    state_path = tmp_path / "s.json"
    write_state_file(
        state_path, base_url="http://127.0.0.1:4100", session_id="ses1",
        server_pid=99999999, worktree=str(tmp_path), launched_at="2026-09-17T08:00:00+00:00", ttl=86400,
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
        server_pid=99999999, worktree=str(tmp_path), launched_at="2026-09-17T08:00:00+00:00", ttl=86400,
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
        server_pid=99999999, worktree=str(tmp_path), launched_at="2026-09-17T08:00:00+00:00", ttl=86400,
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"] + sys.argv[1:])
