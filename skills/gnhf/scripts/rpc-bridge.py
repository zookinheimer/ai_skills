#!/usr/bin/env python3
# Bridges gnhf's file-based monitoring protocol to a `pi --mode rpc`
# subprocess, so an unattended pi run has a live channel to receive an
# answer to a MANUAL_RUN: ASK -- question instead of running deaf like a
# plain `pi -p` launch. Also auto-answers the plan-mode extension's
# "Execute the plan" dialog (see scripts/pi-extensions/plan-mode/), so a
# --plan run doesn't stall waiting for a human that isn't there.
#
# Status-file vocabulary (first line of --status-file, atomically written):
#   RUNNING        pi subprocess started, no terminal outcome yet
#   ASK            model printed MANUAL_RUN: ASK -- ; waiting on --control-file
#   DONE           model printed MANUAL_RUN: DONE --
#   BAILED         model printed MANUAL_RUN: BAILED --
#   PROCESS_EXITED pi exited before ever printing a terminal marker
#   BRIDGE_ERROR   this script hit an unhandled exception
#   KILLED         this script received SIGTERM (TTL expiry or external kill)
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

MARKER_RE = re.compile(r"MANUAL_RUN:\s*(DONE|BAILED|ASK)\s*—\s*(.*)", re.DOTALL)

# Control-file command types passed straight through to pi's stdin verbatim,
# beyond the bridge's own synthesized "answer" -> steer/prompt translation.
PASSTHROUGH_TYPES = {
    "prompt", "steer", "follow_up", "get_state", "abort", "abort_bash",
    "abort_retry", "clear_queue", "set_steering_mode", "set_follow_up_mode",
    "set_auto_retry", "set_auto_compaction", "bash",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--session-id", required=True)
    p.add_argument("--prompt-file", required=True)
    p.add_argument("--control-file", required=True)
    p.add_argument("--events-log", required=True)
    p.add_argument("--status-file", required=True)
    p.add_argument("--cwd", required=True)
    p.add_argument("--provider")
    p.add_argument("--model")
    p.add_argument("--pi-bin", default="pi")
    p.add_argument("--extension", action="append", default=[], help="path to load via pi's --extension (repeatable)")
    p.add_argument("--plan", action="store_true", help="start pi in plan mode (passes --plan through)")
    p.add_argument("--poll-interval", type=float, default=1.0)
    return p.parse_args()


class Bridge:
    def __init__(self, args):
        self.a = args
        self.proc = None
        self.is_streaming = False
        self.turn_text = []
        self.control_offset = 0
        self.log_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.terminal_state = None  # "DONE" | "BAILED" once reached
        self.bridge_log_path = self._bridge_log_path()

    def _bridge_log_path(self) -> str:
        base, _ = os.path.splitext(self.a.events_log)
        return base + ".bridge.log"

    # ---------- lifecycle ----------
    def start(self):
        cmd = [self.a.pi_bin, "--mode", "rpc", "--session-id", self.a.session_id]
        if self.a.provider:
            cmd += ["--provider", self.a.provider]
        if self.a.model:
            cmd += ["--model", self.a.model]
        for ext in self.a.extension:
            cmd += ["--extension", ext]
        if self.a.plan:
            cmd += ["--plan"]
        self.append_bridge_log(f"Spawning: {' '.join(cmd)} (cwd={self.a.cwd})")
        self.proc = subprocess.Popen(
            cmd, cwd=self.a.cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
        )
        self.write_status("RUNNING", "")
        prompt_text = open(self.a.prompt_file, encoding="utf-8").read()
        self.send_command({"type": "prompt", "message": prompt_text})

    def run(self):
        threading.Thread(target=self.control_loop, daemon=True).start()
        threading.Thread(target=self.stderr_pump, daemon=True).start()
        self.event_loop()

    # ---------- outbound (bridge -> pi stdin) ----------
    def send_command(self, cmd: dict):
        line = json.dumps(cmd)
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        self.append_log(json.dumps({"bridge_sent": cmd}))

    # ---------- events (pi stdout -> bridge) ----------
    def event_loop(self):
        for raw in self.proc.stdout:
            raw = raw.rstrip("\n")
            if not raw:
                continue
            self.append_log(raw)
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            self.handle_event(event)
            if self.terminal_state:
                self.shutdown_pi()
                break
        self.finish()

    def handle_event(self, event):
        etype = event.get("type")
        if etype == "extension_ui_request":
            self.handle_extension_ui_request(event)
        elif etype == "agent_start":
            self.is_streaming = True
            self.turn_text = []
        elif etype == "message_end":
            msg = event.get("message") or {}
            if msg.get("role") == "assistant":
                for block in msg.get("content") or []:
                    if block.get("type") == "text":
                        self.turn_text.append(block.get("text", ""))
        elif etype == "agent_settled":
            self.is_streaming = False
            text = "".join(self.turn_text)
            state, detail = self.scan_markers(text)
            if state != "SETTLED_NO_MARKER":
                self.write_status(state, detail)
                if state in ("DONE", "BAILED"):
                    self.terminal_state = state
            else:
                # Ordinary mid-run settle (e.g. one plan-execution step) --
                # not an anomaly by itself. Only a settle with no marker
                # that pi then never follows up from (process exits with
                # no terminal_state) is worth flagging; see finish().
                self.append_bridge_log("settled with no MANUAL_RUN marker (not terminal, continuing)")
        elif etype == "response" and event.get("success") is False:
            self.append_bridge_log(f"WARN command failed: {json.dumps(event)}")

    def handle_extension_ui_request(self, event):
        method = event.get("method")
        req_id = event.get("id")
        if method == "select":
            options = event.get("options") or []
            execute_opt = next((o for o in options if o.startswith("Execute the plan")), None)
            if execute_opt is not None:
                self.append_bridge_log(f"auto-answering plan-mode select -> {execute_opt!r}")
                self.send_command({"type": "extension_ui_response", "id": req_id, "value": execute_opt})
                return
            self.append_bridge_log(f"WARN unrecognized select dialog, cancelling: {json.dumps(event)}")
            self.send_command({"type": "extension_ui_response", "id": req_id, "cancelled": True})
        elif method in ("confirm", "input", "editor"):
            self.append_bridge_log(f"WARN unhandled {method} dialog, cancelling: {json.dumps(event)}")
            self.send_command({"type": "extension_ui_response", "id": req_id, "cancelled": True})
        else:
            # fire-and-forget (notify/setStatus/setWidget/setTitle/set_editor_text): no response needed
            self.append_bridge_log(f"extension_ui_request (fire-and-forget): {json.dumps(event)}")

    @staticmethod
    def scan_markers(text: str):
        found = {m.group(1): m.group(2).strip() for m in MARKER_RE.finditer(text)}
        for kind in ("BAILED", "DONE", "ASK"):
            if kind in found:
                return kind, found[kind]
        return "SETTLED_NO_MARKER", ""

    # ---------- control file (monitor -> bridge) ----------
    def control_loop(self):
        while not self.stop_event.is_set():
            self.poll_control_file()
            time.sleep(self.a.poll_interval)
        self.poll_control_file()

    def poll_control_file(self):
        path = self.a.control_file
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            f.seek(self.control_offset)
            chunk = f.read()
        if not chunk:
            return
        idx = chunk.rfind("\n")
        complete = chunk[: idx + 1] if idx != -1 else ""
        if not complete:
            return
        self.control_offset += len(complete.encode("utf-8"))
        for line in complete.splitlines():
            line = line.strip()
            if line:
                self.handle_control_line(line)

    def handle_control_line(self, line: str):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            self.append_bridge_log(f"WARN malformed control line, skipped: {line!r}")
            return
        ctype = parsed.get("type")
        if ctype == "answer":
            message = parsed.get("message", "")
            cmd = {"type": "steer", "message": message} if self.is_streaming else {"type": "prompt", "message": message}
            self.send_command(cmd)
        elif ctype in PASSTHROUGH_TYPES:
            self.send_command(parsed)
        else:
            self.append_bridge_log(f"WARN unknown control command type: {ctype!r}")

    # ---------- status file (bridge -> monitor), atomic ----------
    def write_status(self, state: str, detail: str):
        tmp = f"{self.a.status_file}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(f"{state}\n{utcnow()}\n{detail}\n")
        os.replace(tmp, self.a.status_file)

    # ---------- shutdown ----------
    def shutdown_pi(self):
        # Empirically confirmed: pi --mode rpc exits ~0.2s after stdin
        # closes once settled. Kept SIGTERM/SIGKILL as a fallback in case
        # a future pi version or an unusual run doesn't behave the same.
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()

    def finish(self):
        self.stop_event.set()
        rc = self.proc.wait()
        if not self.terminal_state:
            self.write_status("PROCESS_EXITED", f"pi exited (code {rc}) before a terminal MANUAL_RUN marker")
        sys.exit(0)

    def stderr_pump(self):
        for line in self.proc.stderr:
            self.append_bridge_log("[pi stderr] " + line.rstrip("\n"))

    def append_log(self, line: str):
        with self.log_lock:
            with open(self.a.events_log, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def append_bridge_log(self, line: str):
        with open(self.bridge_log_path, "a", encoding="utf-8") as f:
            f.write(f"{utcnow()} {line}\n")


def main():
    args = parse_args()
    bridge = Bridge(args)

    def on_sigterm(signum, frame):
        try:
            bridge.write_status("KILLED", "bridge received SIGTERM (TTL expiry or external kill)")
        except Exception:
            pass
        sys.exit(143)

    signal.signal(signal.SIGTERM, on_sigterm)

    try:
        bridge.start()
        bridge.run()
    except Exception as exc:
        try:
            bridge.write_status("BRIDGE_ERROR", repr(exc))
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
