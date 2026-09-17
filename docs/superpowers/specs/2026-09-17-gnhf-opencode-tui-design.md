# gnhf: opencode-TUI-driven unattended runs

Date: 2026-09-17
Status: approved, pending implementation plan

## Context

`gnhf` currently resolves `pi` as its default agent and drives unattended
runs through `scripts/rpc-bridge.py`, a bridge around `pi --mode rpc` that
adds a live ASK channel and a vendored plan-mode extension
(`scripts/pi-extensions/plan-mode/`) whose `isSafeCommand` regex check has
needed nine separate false-positive fixes so far (documented in
`SKILL.md`'s "Bundled scripts" section). Upstream
(`pythoninthegrass/ai_skills`) has since replaced its own equivalent with a
simpler unified `scripts/gnhf.py` (PEP 723 `uv run --script`, `-s`
smoke-test / `-l` launch modes, `python-decouple` config layering, 429
rate-limit-aware backoff/retry with distinct `RATE_LIMITED`/`EARLY_EXIT`
exit codes) that supports both `pi` and `opencode`, but only in headless
mode (`opencode run`).

The user wants two changes: adopt upstream's simpler script shape and its
429-survival behavior, and switch the default/primary agent from `pi` to
**opencode, with the actual interactive TUI as the visible session** (not
headless `opencode run`) — while still driving it through the most
reliable and efficient control path available, run inside a Herdr pane per
the existing convention for agent visibility.

## Key discovery

`opencode serve` exposes a full REST API (`GET /doc` on the running server
returns its OpenAPI 3.1 spec) that a running `opencode attach <url>` TUI
session also renders live. This means automation and a human-visible TUI
can be two independent clients of the same session — our script never
needs to screen-scrape a terminal, and the TUI stays genuinely interactive
and attachable at any time. This supersedes rpc-bridge.py's live-ASK
channel (a bespoke pi-only mechanism) with an off-the-shelf capability
that works the same way for a human sitting down at the TUI as for our
script.

It also exposes a session-scoped `permission` ruleset at session-creation
time (`POST /session`, `{permission, pattern, action}` triples) — this
supersedes the vendored plan-mode extension entirely. Instead of pattern-
matching raw shell strings (the source of all nine documented false-
positive bugs), permission decisions are structured objects the server
itself evaluates before a tool call ever reaches the model's turn.

## Architecture

```
                    ┌─────────────────────────┐
   Herdr pane  ──────►  opencode attach <url>  │   (human-visible TUI,
   (human-visible)  │  (real interactive TUI)  │    optional to watch)
                    └───────────┬─────────────┘
                                │ same session,
                                │ both are HTTP clients
                                ▼
                    ┌─────────────────────────┐
                    │   opencode serve         │   (detached, in the
                    │   --port <port>          │    worktree directory)
                    └───────────┬─────────────┘
                                │ REST API
                                ▼
                    ┌─────────────────────────┐
   gnhf.py  ─────────►  requests / urllib      │   (our automation:
   (background,       │  POST /session          │    create, prompt,
    polling loop)     │  POST .../message        │    monitor, abort)
                    └─────────────────────────┘
```

## Components

### 1. `scripts/gnhf.py` (rewritten)

Keeps upstream's overall shape — a single self-contained `uv run --script`
tool, `python-decouple` config layering (CLI flag > env > `.env` >
hardcoded default), 429 probe/backoff/retry — but gains a third mode
alongside `-s`/`--smoke-test` and generalizes `-l`/`--launch`:

- `-s/--smoke-test <pi|opencode>` — unchanged in spirit. For `opencode`,
  probes via the server API (start a throwaway `opencode serve`, create a
  session, send the smoke-test prompt, confirm the reply, tear down) rather
  than `opencode run`, so the smoke test exercises the same code path the
  real launch uses.
- `-l/--launch` — for `opencode`, this no longer runs `subprocess.Popen` on
  an agent command. It instead:
  1. Starts `opencode serve --port 0 --hostname 127.0.0.1` detached in the
     target worktree directory, parses the bound port from its startup
     log line (`opencode server listening on http://127.0.0.1:<port>`).
  2. `POST /session` with our default gnhf permission ruleset (see below),
     `directory` scoped to the worktree.
  3. If `HERDR_ENV=1`: create a Herdr pane running
     `opencode attach http://127.0.0.1:<port>` so the run is watchable —
     matching the existing convention for other agents in this skill.
  4. Sends the task prompt to the session (exact endpoint — sync
     `.../message` vs. `.../prompt_async` — confirmed against a live
     server during implementation, not assumed from the spec alone).
  5. Records `{server_pid, session_id, port, worktree}` to a state file
     next to the log, for the monitor step and for `--ttl` enforcement to
     find later.
  6. The existing probe-window + 429-backoff logic applies to step 2-4 as
     a unit: if session creation or the first prompt attempt shows a
     rate-limit signal, back off and retry the whole `serve` + `session`
     sequence, same exit-code contract as today (`RATE_LIMITED`=3,
     `EARLY_EXIT`=4 if it dies for a non-429 reason inside the probe
     window).
  `pi` keeps its existing headless `pi -p` launch path unchanged — this
  redesign is additive for opencode, not a removal of pi support.
- New: `--poll` (invoked by the monitor step, not a separate CLI mode) —
  reads the state file, polls `GET /session/{id}/message` for new
  assistant content (scanning for `MANUAL_RUN: DONE —`/`BAILED —`) and
  `GET /permission` for pending requests outside the ruleset (replies
  `reject` to each), returns a status line the monitor step's existing
  `ScheduleWakeup`-driven check-back loop already knows how to read
  (mirrors today's `.status` file contract from `rpc-bridge.py`, so
  SKILL.md's step 6 table of states needs minimal rewording, not a
  redesign).
- TTL enforcement: the monitor step compares elapsed time since
  `LAUNCHED:` against `--ttl`, and on expiry calls `POST
  /session/{id}/abort` (a clean session cancel — the `opencode serve`
  process and worktree are left running/intact for review, unlike
  `timeout` killing a whole process tree today).

### 2. Default gnhf permission ruleset

Adapted from `~/git/timecard/opencode.jsonc`'s example policy, passed as
the session's `permission` field at creation (not written to any
`opencode.jsonc` file — scoped to just this run, zero risk of leaking into
normal opencode usage in that worktree):

- `edit`: allow
- `webfetch`: allow
- `bash`: `"*"` allow, then explicit `deny` overrides for: force-push,
  hard reset, branch checkout/switch, disk-destroying commands (`dd`,
  `mkfs`, `fdisk`, `parted`, `diskutil erase*`, `newfs*`), privilege
  escalation (`sudo`), system power (`shutdown`/`reboot`/`halt`), process
  kills (`killall`/`pkill`), and `curl|wget ... | sh` piping.

Every `ask` in the timecard example becomes `deny` here — there is no
human to answer an unattended prompt, and letting a policy gap silently
hang the run for the rest of its TTL is worse than a hard deny. Anything
this ruleset doesn't cover still surfaces as a real, structured pending
permission request (`GET /permission`), which the poll step auto-rejects
— this is the safety net that replaces plan-mode's regex scanning, and it
can't have the same class of bug because it's never matching against a raw
command string in the first place.

### 3. Agent resolution (SKILL.md step 1)

Unchanged mechanism, `pi opencode claude codex copilot` still probed via
`which`; only the tie-break default changes from `pi` to `opencode` when
more than one CLI is present and nothing else disambiguates.

### 4. Deprecated/removed

- `scripts/rpc-bridge.py`, `scripts/rpc-bridge-smoke-test.sh`,
  `scripts/pi-extensions/plan-mode/` — removed. Their pi-specific live-ASK
  and plan-mode-auto-execute jobs are both superseded by opencode's native
  session API and permission system. `pi` remains a supported secondary
  agent (smoke-test only, via its existing plain `pi -p` headless path);
  it simply no longer gets the bespoke bridge machinery, matching how
  upstream's own `gnhf.py` already treats it.
- `scripts/mcp-defaults.json`'s godot/context7 wiring — kept, but
  reattached to the new launch path (passed through to `opencode serve`
  via its own MCP config, not `pi`'s `--mcp-config` flag).

## Data flow (launch → finish)

1. `gnhf.py -l --agent opencode -C <worktree> -o <log> -T <ttl>` — starts
   `opencode serve`, creates the session with the gnhf ruleset, opens the
   Herdr pane (if applicable), sends the prompt, writes the state file,
   prints `LAUNCHED: session=... port=... pid=... ttl_expires=...`.
2. SKILL.md step 6 (unchanged cadence) calls `gnhf.py --poll <state-file>`
   on each `ScheduleWakeup` tick. Poll reads new messages and pending
   permissions, replies `reject` to anything outside the ruleset, and
   prints one of `RUNNING` / `DONE` / `BAILED` / `TTL_EXPIRED` (aborts the
   session first if TTL exceeded) — same shape as today's `.status` file,
   just server-backed instead of bridge-backed.
3. SKILL.md step 7 (unchanged) reviews the worktree diff and AC exactly as
   today; the session/server teardown is a poll-step side effect, not part
   of review.

## Error handling

- **429 / rate limit**: identical backoff/retry contract as today's
  `gnhf.py`/`rpc-bridge.py` (`RATE_LIMITED` exit code, never a task
  verdict).
- **`opencode serve` dies inside the probe window**: `EARLY_EXIT`, same as
  today.
- **A pending permission request outside the ruleset**: auto-`reject`ed
  every poll tick, logged, never silently hangs the run.
- **Herdr pane creation fails / `HERDR_ENV` unset**: launch proceeds
  without a visible pane — server + session automation is the load-bearing
  path either way, the pane is a visibility convenience, matching today's
  existing fallback behavior for non-Herdr environments.
- **Server process dies mid-run** (not TTL, not a clean abort): poll step
  reports a distinct `SERVER_DIED` status (new — today's bridge has
  `PROCESS_EXITED`/`BRIDGE_ERROR` for the analogous case), treated the same
  as a `BAILED`-without-marker: don't push, report the blocker.

## Testing

- `scripts/test_gnhf.py` (ported from upstream, extended): exercises the
  429 backoff/retry logic against a fake server, the permission-ruleset
  payload shape, and the poll step's marker-scanning — all mockable
  without a real `opencode serve` process.
- One live smoke test against a real local `opencode serve` (documented as
  a manual pre-merge check, not part of the automated suite, since it
  needs a real model backend) confirming: session create with ruleset →
  prompt → a deliberately out-of-policy bash command gets a real pending
  permission request → poll step rejects it → session aborts cleanly on a
  short TTL.

## Open items to confirm during implementation (not blocking design approval)

- Exact endpoint for sending the prompt: the live server's `/doc` listed
  both an unversioned `POST /session/{id}/message` and a `/api/v2` route
  set (`POST /api/session/{sessionID}/prompt`) with overlapping
  `operationId`s (`session.prompt` vs `v2.session.prompt`). Verify against
  a live server which is current before wiring it into `gnhf.py`, rather
  than assuming.
- Whether `opencode serve` infers its project directory from its own `cwd`
  (matching `opencode`'s TUI default) or needs an explicit directory param
  on `POST /session` — verify live rather than assume from the spec.
