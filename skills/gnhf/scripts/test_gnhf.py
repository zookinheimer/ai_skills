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


if __name__ == "__main__":
    pytest.main([__file__, "-v"] + sys.argv[1:])
