#!/usr/bin/env bash
# Exercise rpc-bridge.py end to end against a real `pi --mode rpc`
# subprocess, in two scenarios:
#   1. ASK/answer loop: force a MANUAL_RUN: ASK --, answer it via the
#      control file, confirm MANUAL_RUN: DONE -- is reached.
#   2. Plan-mode auto-execute: launch with --plan and the vendored
#      plan-mode extension, confirm the bridge auto-answers the
#      "Execute the plan" dialog with no control-file write, and
#      MANUAL_RUN: DONE -- is still reached.
# Exit 0 = both loops worked, non-zero = one didn't (see diagnostics).
set -uo pipefail   # not -e: we poll in loops and inspect status ourselves

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE="$SCRIPT_DIR/rpc-bridge.py"
EXTENSION="$SCRIPT_DIR/pi-extensions/plan-mode/index.ts"
TIMEOUT=120

usage() {
  cat <<'EOF'
Usage: rpc-bridge-smoke-test.sh [--timeout SECS]

Options:
  --timeout SECS   per-scenario wall-clock bound for the pi subprocess (default: 120)
  -h, --help       show this help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --timeout) TIMEOUT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if ! command -v pi >/dev/null 2>&1; then
  echo "FAIL: pi is not on PATH" >&2
  exit 1
fi

WORKDIR="$(mktemp -d)"
BRIDGE_PID=0
cleanup() {
  # BRIDGE_PID=0 means "nothing to kill" -- `kill 0` would signal this
  # script's whole process group instead, which is not what we want here.
  [ "$BRIDGE_PID" -ne 0 ] 2>/dev/null && kill "$BRIDGE_PID" 2>/dev/null
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

fail() {
  local label="$1" status="$2"
  echo "FAIL($label): $status" >&2
  echo "--- status file ---" >&2; cat "$WORKDIR/$label.status" 2>/dev/null >&2
  echo "--- tail events log ---" >&2; tail -n 60 "$WORKDIR/$label.log" 2>/dev/null >&2
  echo "--- tail bridge log ---" >&2; tail -n 60 "$WORKDIR/$label.bridge.log" 2>/dev/null >&2
  exit 1
}

wait_for_status() {   # wait_for_status LABEL WANT_STATE BUDGET_SECS
  local label="$1" want="$2" budget="$3" waited=0
  while (( waited < budget )); do
    if [ -f "$WORKDIR/$label.status" ] && [ "$(head -n1 "$WORKDIR/$label.status")" = "$want" ]; then
      return 0
    fi
    kill -0 "$BRIDGE_PID" 2>/dev/null || { fail "$label" "bridge process died before reaching $want"; }
    sleep 1; ((waited++))
  done
  return 1
}

### Scenario 1: ASK -> answer via control file -> DONE
run_scenario_1() {
  local label="ask"
  cat > "$WORKDIR/$label.prompt.md" <<'EOF'
You are in a test harness, not a real task. Do not use any tools.
Pick a favorite integer between 1000 and 2000 -- since nothing settles
which one, treat this as STUCK POLICY's genuine-ambiguity case: print a
line starting exactly "MANUAL_RUN: ASK — " with your question, then end
your turn and wait. When a later message tells you which number to use,
print a line starting exactly "MANUAL_RUN: DONE — chose <number>" and
stop.
EOF

  timeout "$TIMEOUT" "$BRIDGE" \
      --session-id "rpc-bridge-smoke-$label" \
      --prompt-file "$WORKDIR/$label.prompt.md" \
      --control-file "$WORKDIR/$label.control.jsonl" \
      --events-log "$WORKDIR/$label.log" \
      --status-file "$WORKDIR/$label.status" \
      --cwd "$WORKDIR" \
      >"$WORKDIR/$label.bridge-stdout.log" 2>&1 &
  BRIDGE_PID=$!

  wait_for_status "$label" ASK 60 || fail "$label" "never reached ASK within 60s"
  echo "PASS(1a): reached MANUAL_RUN: ASK --"

  printf '%s\n' '{"type":"answer","message":"Use 1013."}' >> "$WORKDIR/$label.control.jsonl"

  wait_for_status "$label" DONE 60 || fail "$label" "never reached DONE within 60s after answering"
  grep -q '1013' "$WORKDIR/$label.status" || fail "$label" "DONE status did not reflect the answer (expected 1013)"
  wait "$BRIDGE_PID" 2>/dev/null; BRIDGE_PID=0
  echo "PASS(1b): answer via control file reached MANUAL_RUN: DONE --"
}

### Scenario 2: --plan auto-execute -> DONE, no control-file write
run_scenario_2() {
  local label="plan"
  cat > "$WORKDIR/$label.prompt.md" <<'EOF'
You are in a test harness, not a real task. You are in plan mode. When
asked to create a plan, output exactly this (and nothing else):

Plan:
1. Print the word done

When later asked to execute the plan, for step 1: print a line
containing exactly "[DONE:1]", then on its own line print exactly
"MANUAL_RUN: DONE — printed done". Do not use any tools.
EOF

  timeout "$TIMEOUT" "$BRIDGE" \
      --session-id "rpc-bridge-smoke-$label" \
      --prompt-file "$WORKDIR/$label.prompt.md" \
      --control-file "$WORKDIR/$label.control.jsonl" \
      --events-log "$WORKDIR/$label.log" \
      --status-file "$WORKDIR/$label.status" \
      --cwd "$WORKDIR" \
      --extension "$EXTENSION" \
      --plan \
      >"$WORKDIR/$label.bridge-stdout.log" 2>&1 &
  BRIDGE_PID=$!

  wait_for_status "$label" DONE 90 || fail "$label" "never reached DONE within 90s"
  wait "$BRIDGE_PID" 2>/dev/null; BRIDGE_PID=0

  if [ -f "$WORKDIR/$label.control.jsonl" ] && [ -s "$WORKDIR/$label.control.jsonl" ]; then
    fail "$label" "control file was written to, but scenario 2 expects a fully automatic auto-execute with no control-file involvement"
  fi
  grep -q 'auto-answering plan-mode select' "$WORKDIR/$label.bridge.log" || \
    fail "$label" "bridge log has no record of auto-answering the plan-mode select dialog"
  echo "PASS(2): plan-mode select dialog auto-answered, MANUAL_RUN: DONE — reached with no control-file write"
}

run_scenario_1
run_scenario_2
echo "PASS: both rpc-bridge loops verified"
