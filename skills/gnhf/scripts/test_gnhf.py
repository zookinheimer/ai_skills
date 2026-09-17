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

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"] + sys.argv[1:])
