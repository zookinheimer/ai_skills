---
name: gnhf
description: >
  Launch a bounded, low-supervision overnight (or long-unattended) coding
  agent run against one well-specced task, in an isolated worktree, using
  whichever agent CLI and model is currently configured — not a hardcoded
  one. By default, once a task merges it automatically chains to the next
  eligible backlog task (pass --single to disable). Use when the user
  says "gnhf", "good night have fun", "run this overnight", "let an agent
  work on this while I'm away/asleep", "burn this task unattended", or
  asks to set up a long-running agent session bounded by a time or turn
  budget with minimal check-ins.
argument-hint: "<task-id-or-description> [ttl] [max-turns] [--single]"
---

# gnhf

## Resolve `SKILL_DIR` (do this before running the bundled script)

`scripts/smoke-test.sh` is a direct sibling of this file in every install
layout. Set `SKILL_DIR` to the absolute path of the directory containing
THIS SKILL.md you just Read (your harness told you that path in the Read
result), e.g.:

```
Read ~/.claude/skills/gnhf/SKILL.md → SKILL_DIR=~/.claude/skills/gnhf
Read .agents/skills/gnhf/SKILL.md   → SKILL_DIR=.agents/skills/gnhf
```

Note: this is a personal, from-scratch skill, unrelated to the `gnhf` npm
package (`kunchenguid/gnhf`) that also occupies this name. Don't adopt that
tool's CLI, config file, or Companion/Hands-Off skill — this one is simpler
and modeled on how this user actually runs unattended agents (manual git
worktree, a Herdr-managed pane when available (see step 5) or a plain
background process otherwise, `timeout` for the wall-clock bound either
way).

## Overview

One agent, one task, one isolated worktree, bounded by a TTL (wall clock)
or a turn count, minimally supervised until it finishes, bails, or the
bound expires. The agent CLI and model must be resolved from whatever is
currently configured — never hardcode a specific agent/model pairing,
because both get rotated over time.

Every environment-specific path mentioned below (config repos, worktree
conventions, prompt templates) is a signal to check *if present*, not a
requirement. Test for existence before reading; treat a missing one as
"skip and fall through," not a failure. This is what keeps the skill
working on a machine that doesn't have this user's particular config repos
checked out.

**Chaining to the next task is the default.** Once one task's cycle ends
(merged, or a bail/escalation the user has seen and responded to), don't
stop — go back to step 2, pick the next eligible task, and repeat steps
2-7 for it. Keep going until no eligible task remains, the user stops it,
or genuine progress requires the user (see step 8). Pass `--single`, or
have the user say "just this one," to run exactly one task and stop
normally at step 7 instead. Chaining is strictly serial — one agent, one
task, one worktree at a time, the same as a single run; the point is
unattended endurance across many tasks, not concurrency. Step 8 covers
the chaining mechanics in full, including how monitoring has to work
differently to survive across tasks unattended.

## 1. Resolve the current agent + model (don't hardcode)

Resolution order — try the CLI's own current default FIRST, unconditionally;
only fall back to inspecting config repos if that default doesn't actually
work. Don't reverse this: checking `pi_config`/`opencode_config` before ever
trying the CLI's own default means testing a config the CLI might not even be
using, and skips over whatever it's already pointed at (some qwen3.8 variant,
another open-weight model, whatever) without ever asking it.

1. `which pi opencode claude codex copilot 2>/dev/null` — which agent CLIs
   are actually on `PATH` right now. If none are found, stop here and
   report that no supported agent CLI is installed; don't guess or try to
   install one. Default to `opencode` if more than one is found and
   nothing else disambiguates (matches this user's usual setup — opencode
   runs as a genuinely interactive TUI, attachable live via a Herdr pane,
   see step 5).
2. **First pass — the CLI's own unmodified default.** Smoke-test it with no
   `--provider`/`--model` overrides:

   ```bash
   "${SKILL_DIR}/scripts/gnhf.py" -s pi
   # or:
   "${SKILL_DIR}/scripts/gnhf.py" -s opencode
   ```

   Whatever the CLI does with zero model flags *is* "currently configured"
   on this machine — don't second-guess it by reading a config repo first.
   If this passes, you're done resolving: confirm which model actually
   served the request via the CLI's own introspection (`pi --list-models`
   marks the active default; opencode's model can be read back from
   `opencode.jsonc`/`opencode models` or from the run's own session
   metadata) so you can report a concrete model name, not just "default."
3. **Only if step 2 fails** (non-zero exit, timeout, or wrong reply — read
   the smoke-test's own tail-of-output diagnosis first, this step is not
   for "it was slow"): inspect the config repos below, each existence-gated
   — a missing path is routine, not an error, just "skip this signal, fall
   through to the next one." These are this user's personal setup, not a
   guaranteed environment; none may exist on a collaborator's machine or a
   fresh install, and if none do, that itself is the bail condition (step
   5).
   - If `~/git/pi_config` exists (`test -d ~/git/pi_config`): read `.env`
     (or `~/.pi/agent/.env` if already rendered) for `PI_DEFAULT_PROVIDER` /
     `PI_DEFAULT_MODEL`, falling back to the "Active default is set per
     machine via..." line in `AGENTS.md` if `.env` alone doesn't explain
     the setup (e.g. multi-machine defaults).
   - If `~/git/opencode_config` exists: read `opencode.jsonc.tpl`'s `model`
     / `agent.build.model` field for opencode's current default.
   - If `~/git/tailscale_config` exists: check `aperture.hujson` (or
     whatever gateway config is live) for which model IDs a shared
     remote/self-hosted backend actually serves right now — a model
     referenced elsewhere may have been rotated out, which is itself a
     likely explanation for why step 2 failed.
4. **Retry the smoke test** with whatever explicit provider/model step 3
   turned up. Current Aperture model IDs: `qwen3.8-flash-next-iq4`,
   `qwen3.5-9b`. The `:builder` suffix is retired — `qwen3.8-flash-next-iq4`
   is a single routable model ID.

   ```bash
   "${SKILL_DIR}/scripts/smoke-test.sh" pi --provider aperture --model "qwen3.8-flash-next-iq4"
   "${SKILL_DIR}/scripts/smoke-test.sh" opencode --model "aperture/qwen3.8-flash-next-iq4"
   ```

   If this passes, resolve to this explicit value for the real run. If it
   fails, retry against `qwen3.5-9b` before falling through to step 5.
5. **Only if step 4 also fails, or step 3 found no config repos at all**:
   bail out cleanly. Report the specific blocker (which command failed, what
   it printed) rather than launching the real task on a guess.

Always say which agent+model you resolved and from where ("CLI default, no
override needed" vs. a specific config-repo file), so a stale assumption is
easy to catch.

Each smoke-test invocation sends one prompt, waits up to `--timeout` seconds
(default 300 — observed cold-start latency on a fresh backend has run into
minutes; don't treat a slow first response as a functional failure), and
checks for the expected reply. Exit 0 with `PASS: ...` means that candidate
works. A non-zero exit means don't use that candidate — read the tail of
output it prints to understand why (agent not installed, backend
unreachable, wrong model id) before moving to the next step.

## 2. Pick a task that's actually a good candidate for this

Do not launch against just any open task. Require, in order:

- **Well-specced**: concrete Acceptance Criteria, not vague/exploratory
  ones; the Description pins down scope with specifics (file paths, line
  ranges, function/symbol names) rather than "figure out a good approach."
  If a real candidate task isn't there yet, that's normal, not a reason to
  stop — see "Research and decide" below.
- **Prior art**: a structurally similar task has already landed in this
  repo — check recent commits / closed tasks with the same label or
  pattern. If nothing comparable has ever been done successfully, this is
  not yet a good unattended-run candidate as written; narrow it until it
  is (see "Research and decide" below) rather than running it supervised
  instead — supervised is the fallback only when the user isn't chaining.
- **Never touch gating/verification tooling**: the one absolute, non-
  negotiable reject condition. If a task cannot be attempted without
  relaxing or editing the repo's own gates (`tools/validate_*`, any
  `task game:*`/equivalent target, lint config, CI), that candidate is
  out, full stop — no research or decision-making gets around this one.

Reject and pick something else only for the gating-tooling condition.
Everything else below ("well-specced", "prior art") is a scoping problem
to solve, not a reason to reject — that's what "Research and decide" is
for.

**Research and decide, don't stop the chain over a design/mechanism
question.** A task needing a *value* pick (a damage number, a cost, a
stacking policy) was already solved by AD-003.06 onward: pick the smallest
defensible candidate with some textual/content-reference source, name it
candidate-not-parity, move on. Apply that same discipline one level up,
to *mechanism* questions (how does a cadence work, what's the shape of a
new subsystem, how should a vague two-line AC decompose into slices) —
research it with the same rigor you'd want from the launched run itself:
read every doc/fixture the task references and any it doesn't but plainly
bears on it, check how this codebase already resolved a structurally
similar ambiguity (grep prior task Notes sections — they are a decision
log), and pick the option best supported by what you find. Write the
decision and its justification into the new subtask's Description exactly
like a launched run writes one into its own Notes, so it's reviewable, not
just asserted. If the first reasonable slice is still too big once you've
made the mechanism decision, split again — recursively, as many times as
it takes to reach something the step 2 checklist actually passes. None of
this is a reason to stop and ask; it's the work chaining is for. The
*only* thing that still stops the chain over a task's own content is the
gating-tooling condition above — see step 8 for the complete, narrow list
of real stops.

Find the task through whatever this repo actually uses (Backlog.md MCP
task, `TODO.md` entry, a GitHub issue) — don't assume one system across
repos. **If the repo's own instructions (e.g. `AGENTS.md`) say task
markdown should only be edited through a Backlog MCP/CLI tool, check that
the tool actually resolves (`which backlog`, or watch for an `ENOENT` MCP
connection failure) before falling back to direct `Read`/`Write`/`Edit` on
the task files** — a silent, permanent fallback is exactly how this repo's
own gap went unnoticed for an entire epic (found and closed 2026-09-11:
`.mcp.json`/`.codex/config.toml` both expected a bare `backlog` binary on
`PATH` that was never installed; `npm install -g backlog.md` closed it).
Direct markdown edit is still the correct fallback when the tool genuinely
isn't available — just don't let its absence go unremarked when a stated
project convention exists to use something else instead.

**Picking the next task automatically while chaining (step 8).** Don't
stop to ask the user which task comes next. Walk the backlog in its own
priority/ordinal order, and **exhaust the current task family before
moving to the next one**: if you were handed (or previously split out of)
AD-NNN.MM, keep working through AD-NNN.MM's remaining slices — split
further, research mechanism questions, whatever it takes — until that
whole parent is genuinely done, before advancing to AD-NNN.(MM+1) or the
next top-level AD-(NNN+1). Run this checklist against each candidate in
turn until one passes.

If a candidate fails the checklist for being too broad/vague (concrete
Acceptance Criteria missing, no prior art), don't stop to ask what to do
about it either — apply the recommended fix yourself: split it into a
narrower, concretely-scoped first subtask the way AD-003.07 was split
into AD-003.07.01 (mirror the specificity of an already-landed sibling
task, name in the new subtask exactly what's deferred and why), leave a
short note on the parent task pointing at the split, then launch on that
subtask instead of the original. If the broadness is because the slice
needs a mechanism decision, not just a narrower value pick, that is not a
reason to reject it either — see "Research and decide" above: make the
call, document it in the new subtask, and proceed the same way.

The only thing chaining still stops and asks a person about, for a
task's own content, is the gating-tooling condition above (needs to
relax or edit the repo's own gates, no matter how it's split or
redesigned). Everything else — including "this needs a real design
decision" — gets solved by research and a documented choice, not by
escalating. See step 8 for the complete, narrow list of real stops
(it is not this one).

**Standing user preference (set 2026-09-11): "no small reuse candidate
anywhere in what's left" is not a stop condition either — it is the exact
same "research and decide" situation, one level up, and the same recursive
split applies.** This is a real incident, not a hypothetical: a chain
researched every remaining item in a task family, found each one needed a
genuinely new subsystem (not just a magnitude to invent), and paused to
ask the user whether to keep going rather than picking one and splitting
it. The user's answer: keep going, always — "needs a new subsystem" was
never one of the four real stops, and stopping to ask was itself the bug,
not a reasonable caution. The corrected procedure, to use automatically
from here on with no exception:

1. **Re-check every remaining candidate for a reuse opportunity before
   concluding none exists.** "Needs a collar system" turned out to mean
   "needs one boolean field already built two slices ago" three separate
   times in this epic's own history (AD-003.08.13/.14/.16), and a fourth
   candidate assumed to need a whole spell system turned out to reuse an
   existing command kind wholesale (AD-003.08.17) — once *after* a user
   had to say so explicitly. Do this check yourself, unprompted, before
   ever concluding "nothing small is left."
2. **If that search genuinely comes up empty for every remaining item**,
   pick the best candidate by the same tie-breaks already used everywhere
   else (best evidence tier; smallest total blast radius; most existing
   structure to build on) and treat "build the subsystem" as the mechanism
   decision from "Research and decide" above, scaled up. Recursively split
   *that* decision the same way an oversized task's own AC gets split:
   name the smallest first sub-slice of the subsystem that is itself
   well-specced and buildable (a minimal primitive, not the whole thing —
   the same way a real spell system would eventually start from one
   working spell, not all of them at once), write it up with the same
   research rigor as any other subtask, and launch it. A subsystem this
   large may take several further slices after the first; that is normal,
   not a sign something went wrong.
3. **Do not call `AskUserQuestion` (or any other check-in) over "should I
   keep going, this is a big one."** That question is already answered:
   yes. The only things that still warrant a stop are the four listed at
   the end of step 8 — a task genuinely needing zero candidate information
   at all (not even a value to invent — `change-formation`'s doc row
   naming no candidate values whatsoever is the one real example so far)
   is closer to "no eligible task" than to "found nothing small"; skip it
   for the next candidate the same way a gating-tooling conflict gets
   skipped, without asking.

## 3. Isolate in a worktree

Create a dedicated git worktree for this run rather than working in the
main checkout or an existing feature branch. Follow the repo's own
established convention if it has one; otherwise a plain sibling directory
named after the task/branch is fine:

```bash
git worktree add /path/to/worktrees/<TASK-ID> -b <TASK-ID>
```

**In this repo specifically**: use `~/git/manual-wt/<TASK-ID>` (and
`~/git/manual-wt/logs/` for the prompt/events/status/control files) — NOT
`~/git/worktrees/`, which belongs to the separate `burnkit`/`dsh`
automated driver (`scripts/burn/driver.py`; see
`docs/development.md#autonomous-dsh-headless-sessions`). The two drivers
don't share worktree state or a lock protocol with each other, so keeping
them in visibly separate sibling directories is what actually prevents a
collision, not a naming convention alone. Confirmed as the real path in
practice, not just a placeholder: AD-003.08.04 (2026-09-10) ran from
`~/git/manual-wt/AD-003.08.04`.

If the model backing this run is local/self-hosted (nothing leaves the
box), and the task needs gitignored source material the repo normally
keeps out of remote-model reach, symlink those directories into the
worktree from the main checkout. If the model is a remote/hosted one,
don't — respect whatever privacy boundary the repo documents (this is
usually spelled out per-backend, e.g. a "local vs remote planner" split in
existing driver prompt templates).

## 4. Build the prompt

Prefer the repo's own prompt conventions if any exist (grep for something
like a driver/burn prompt template) and adapt it rather than inventing new
rules from scratch. Otherwise use this minimal skeleton. **If the resolved
agent (step 1) is `pi`**, use the ASK-aware variant below — step 5 launches
it through `rpc-bridge.py`, which gives it a real (if narrow) live channel
back. For every other resolved agent, use the template exactly as it reads
in the "non-pi variant" section — unchanged, no live channel exists for
those.

### pi variant (ASK-aware)

Identical to the non-pi skeleton below except for the framing paragraph and
STUCK POLICY:

```text
You are working alone in this repository checkout, a dedicated git
worktree on branch <TASK-ID>. Complete exactly ONE task, given in full
below, end to end. An orchestrator is watching this session
asynchronously and CAN research and answer a live question — but it isn't
a person, knows nothing about this task beyond what you tell it, and
won't check in on a fixed schedule; treat that exactly like the "nobody is
watching" case below, except for the one narrow case STUCK POLICY's
second bullet describes. If you find yourself wanting to ask a clarifying
question or unsure which of several reasonable interpretations to pick,
that feeling is not by itself grounds to ask — resolve it yourself from
the repo/task evidence whenever you genuinely can. Reserve the ASK marker
strictly for the same rare case that would otherwise be a BAIL.

HARD RULES:
- Never relax or edit this repo's own verification/gating tooling to make
  a stuck task pass. If something genuinely can't be verified with the
  existing gates, that's a BAIL — name the blocker and stop.
- Never fabricate a passing check you did not just run. If a tool/pipeline
  fails repeatedly and you work around it by hand, say so plainly — that's
  an accepted outcome here, but claiming a run that didn't happen is not.
- Match the surrounding code/prose style; comments explain WHAT or WHY,
  never "improved"/"fixed"/"new".

STUCK POLICY — two distinct outcomes, don't guess and don't keep spinning:
- The same blocker persists across ~3 distinct fix attempts with no
  genuine progress, and no genuine ambiguity behind it (just repeated
  failure): BAIL. Write the blocker into the task's notes, print a line
  starting `MANUAL_RUN: BAILED —` describing it, and end your turn.
- A single genuine ambiguity that would change scope or behavior and
  isn't settled by the task text, the referenced docs/fixtures, or
  existing repo precedent (a coin-flip you'd normally ask a human about):
  ASK. Print a line starting `MANUAL_RUN: ASK —` with a concise question
  AND what you already tried/considered/ruled out, then end your turn
  normally — do not also print a BAILED marker in the same breath, ASK and
  BAIL are different outcomes. Ending your turn is what signals the
  question; stop there and wait rather than continuing to work past it. If
  an answer arrives, it comes as the next message in this same session —
  treat it as authoritative and continue. This is not an invitation to ask
  more often: the bar is exactly the same as the BAIL bullet above, only
  the outcome changed from a dead end to a live answer.
In either case: do this BEFORE your last message, never instead of it — a
turn that ends on plain text with no marker is indistinguishable from a
crash to whoever reviews this run later.

FINISH PROTOCOL: when every Acceptance Criteria item is genuinely
satisfied, update the task's status/notes, commit locally on this branch
(no push, no PR — a human reviews the worktree directly), then print a
line starting `MANUAL_RUN: DONE —` summarizing what landed.

Your diff should touch only what this task's Acceptance Criteria describe.

---

<full task description + acceptance criteria, verbatim>
```

### non-pi variant

```text
You are working alone in this repository checkout, a dedicated git
worktree on branch <TASK-ID>. Complete exactly ONE task, given in full
below, end to end. Nobody is watching this session and no one will answer
a question you ask — there is no user to reply. If you find yourself
wanting to ask a clarifying question or unsure which of several reasonable
interpretations to pick, that feeling IS the stuck condition below, not a
reason to stop and wait for input: resolve it yourself from the repo/task
evidence if you genuinely can, and BAIL if you can't — never end your turn
on an open question with no BAIL marker.

HARD RULES:
- Never relax or edit this repo's own verification/gating tooling to make
  a stuck task pass. If something genuinely can't be verified with the
  existing gates, that's a BAIL — name the blocker and stop.
- Never fabricate a passing check you did not just run. If a tool/pipeline
  fails repeatedly and you work around it by hand, say so plainly — that's
  an accepted outcome here, but claiming a run that didn't happen is not.
- Match the surrounding code/prose style; comments explain WHAT or WHY,
  never "improved"/"fixed"/"new".

STUCK POLICY: BAIL (don't guess and don't keep spinning) on either of
these:
- the same blocker persists across ~3 distinct fix attempts with no
  genuine progress, or
- a single genuine ambiguity that would change scope or behavior and
  isn't settled by the task text, the referenced docs/fixtures, or
  existing repo precedent (a coin-flip you'd normally ask a human about).
In both cases: write the blocker/ambiguity into the task's notes, print a
line starting `MANUAL_RUN: BAILED —` describing it, and end your turn.
Do this BEFORE your last message, never instead of it — a turn that ends
on plain text with no marker is indistinguishable from a crash to whoever
reviews this run later.

FINISH PROTOCOL: when every Acceptance Criteria item is genuinely
satisfied, update the task's status/notes, commit locally on this branch
(no push, no PR — a human reviews the worktree directly), then print a
line starting `MANUAL_RUN: DONE —` summarizing what landed.

Your diff should touch only what this task's Acceptance Criteria describe.

---

<full task description + acceptance criteria, verbatim>
```

## 5. Launch, bounded

The wall-clock bound (TTL) is the primary and simplest bound.

**If the resolved agent is `opencode`** (the default): `gnhf.py -l --agent
opencode` starts `opencode serve` detached in the worktree, creates a
session with gnhf's own permission ruleset (allow edit/webfetch/bash,
explicit deny list for destructive bash patterns — no `ask` entries, since
nothing will ever answer one unattended), and sends the task prompt via
the session's async prompt endpoint. Write the prompt to
`<log-path-without-extension>.prompt.md` first — next to the log, not
inside the worktree, so it can never end up in the launched agent's own
`git status`/diff. `gnhf.py` reads it from there and refuses to launch
(`EXIT_USAGE`, a clear stderr message) if the file is missing, rather than
silently launching with an empty prompt and burning the full TTL waiting
for a marker that can never appear:

```bash
cat > /path/to/logs/<TASK-ID>.prompt.md <<'PROMPT_EOF'
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

Default `TTL_SECONDS` to 86400 (24h) unless the user gives a different
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
- `IDLE_NO_MARKER: nudged (attempt N)` — the session went idle without
  printing a marker; gnhf sent one automatic nudge asking the model to
  assess its own progress and print `MANUAL_RUN: DONE`/`BAILED` now. Keep
  polling as normal — this is the mitigation working, not a sign to
  intervene, unless it recurs across multiple separate idle spells.
- `IDLE_NO_MARKER: settled with no MANUAL_RUN marker after N nudge(s);
  needs manual review` — nudging didn't get a marker after `MAX_IDLE_NUDGES`
  attempts (currently 2). Treat like a silent marker-less exit: check the
  worktree's `git log`/`git status` yourself to see what actually
  happened, report it as the blocker, don't relaunch on a guess.
- `POLL_ERROR: ...` — a transient API error (network blip, momentarily
  busy server) during the poll itself, not a verdict on the task. Keep
  polling; if it recurs on every tick, treat it like `SERVER_DIED`.

If a Herdr pane is running the TUI, you can also glance at it directly
(`herdr agent read <pane_id> --source recent-unwrapped --lines 120`) for a
human-readable view of the same session `--poll` is checking
structurally — useful for judging genuine progress vs. thrashing, since
`--poll`'s `RUNNING` alone doesn't distinguish the two.

**If the resolved agent is `pi`**: `pi`'s own launch/monitor script logic
(`run_launch_pi`, `build_smoke_cmd`'s pi branch) is a byte-identical port
of what it always was — but the surrounding tooling is not: this rewrite
drops the `rpc-bridge.py`-based live ASK channel, `.status` file, and
heartbeat that `pi` had as the primary agent before the opencode-server-API
rewrite. `pi` now uses the same plain checks below that were always the
simpler fallback for a non-bridge-monitored agent:

```bash
tail -n 40 /path/to/logs/<TASK-ID>.log
ps -p <PID> -o pid,etime,stat
cd /path/to/worktrees/<TASK-ID> && git log --oneline -5 && git status --short
```

Silent exit (process gone, no `MANUAL_RUN` marker in the log): treat as a
`BAILED`-equivalent, report the blocker from the log's last ~50 lines,
don't relaunch on a guess.

**For both agents**: only intervene (nudge, or kill) on genuine thrashing
signals — the same failing command repeating verbatim, no commits after a
long stretch with the identical error recurring, or an obvious loop. A run
that's slow but making incremental progress is not thrashing — let it
continue. Otherwise let it run until a terminal `--poll` line / log marker
appears, the TTL expires, or you've confirmed real thrashing.

## 7. Finish

On `MANUAL_RUN: BAILED —`, a silent marker-less exit (`pi`; step 6), a
`SERVER_DIED` or exhausted `IDLE_NO_MARKER` poll result (`opencode`; step
6), TTL expiry, or a thrashing kill: don't push anything.

On `MANUAL_RUN: DONE —`: the launched agent commits locally only (per its
own FINISH PROTOCOL, step 4 above) and never pushes or opens a PR itself —
that's deliberate, so nothing goes further before a review step happens.
That review step is yours, not optional, and not the same as trusting the
run's own self-report:

1. `git diff --stat <base>..<head>` — confirm the diff touches only what
   the task's Acceptance Criteria describe (task file, the specific
   ref/tools paths named in the task), not unrelated trees. Confirm any
   untracked entries are pre-existing gitignored symlinks (`analysis`,
   `extracted`), not real content that should've been `.gitignore`d or
   was accidentally staged.
2. Check the task file's Acceptance Criteria are actually checked `[x]`,
   not just claimed done in prose.
3. Once the diff passes review, push the branch and open a PR (`gh pr
   create`, following whatever title/body convention recent merged PRs in
   this repo already use) — then **merge it by default** (`gh pr merge
   --squash`, matching this repo's established merge style) rather than
   leaving it open for a separate human pass. Fast-forward the local
   `main` checkout (`git fetch && git merge --ff-only origin/main`) so the
   next task's worktree branches off a `main` that includes it.

Only skip the push/merge and escalate instead if review turns up a real
problem (scope violation, an unchecked AC, fabricated evidence) — report
that plainly rather than merging over it. Always report back:
DONE/BAILED/TTL-expired/killed-for-thrashing, what actually landed (from
`git log`/`git diff`, not from the run's own self-report), and the PR/merge
outcome.

**Explicitly stop that task's Monitor once you're done with it here —
don't rely solely on the polling loop's own `break` to exit it.** A real
incident (2026-09-11): two Monitor loops (each a plain `while true; do
head -n1 "$f"; case ...; sleep 15; done` polling a status file for
`DONE`/`BAILED`/etc, per step 6) kept running for hours after their tasks
had already finished, been reviewed, and merged — one status file was
observed to read `DONE` (correctly triggering review and merge) and then,
later, `RUNNING` again with no agent process left alive to have written
that, so the loop's `case` never got a second chance to match and `break`.
The exact mechanism wasn't fully pinned down, but the fix does not depend
on knowing it: once you've finished acting on a task in this step (merged,
or escalated a bail/failure) you already have that task's Monitor id in
hand from when you armed it in step 6 — call `TaskStop` on it right here,
unconditionally, rather than assuming the loop already exited or will
exit on its own. This costs nothing when the loop did already exit
cleanly (`TaskStop` on an already-finished task is a harmless no-op) and
prevents exactly this accumulation when it didn't.

**For `opencode` runs, also reap the detached `opencode serve` process
once you're done acting on this task here.** Neither `--poll`'s
`TTL_EXPIRED` path (which cleanly aborts the *session* via the API, not
the server process) nor any other terminal state kills the process
itself — nothing else in this skill does either, so it's left running
indefinitely unless you do this:

```bash
kill "$(python3 -c "import json; print(json.load(open('/path/to/logs/<TASK-ID>.state.json'))['server_pid'])")" 2>/dev/null || true
```

Harmless to run even if the process already exited on its own
(`SERVER_DIED`, or any other reason) — `kill` on an already-gone PID is a
no-op here, not an error worth guarding more carefully than the `|| true`
above.

**Standing user authorization (granted 2026-09-10):** the user has told
Claude directly that it has permission to merge the PR as part of this
skill's own step 7 — `gh pr merge --squash` here is pre-authorized, not
something to pause and re-confirm per task. This does not relax anything
else: still review the diff against the AC first, still only merge a PR
that passed that review, and everything else in this skill's list of real
stops (gating-tooling conflicts, no eligible task, a genuine bail/ASK)
still applies unchanged.

**A merge/push itself can still get blocked by the environment's own
permission classifier**, independent of the authorization above — that
classifier is a separate, harness-level gate that a skill's own text
cannot pre-clear; the standing authorization means don't treat a fresh
merge as needing a *new* conversational confirmation, but if the
classifier itself refuses the action, that is still a hard external stop,
not a decision of yours to route around. Report it plainly (a Bash
permission rule in the user's settings, added via the `update-config`
skill, is the actual fix — offer it rather than retrying the same denied
call) and wait; once resolved, resume the chain (step 8) from exactly
where it stopped rather than re-doing the review.

## 8. Chain to the next task (skip if `--single`)

After step 7 ends in a merge, or in a bail/escalation the user has since
seen and acknowledged: don't stop here. Go back to step 2, auto-pick the
next eligible task (including its "apply the recommended split
automatically" rule), and run steps 2-7 again for it in a fresh
worktree/branch. Reuse the agent+model already resolved in step 1 rather
than re-running the smoke test each time — re-resolve only if a run
itself reports the model/provider is unreachable.

**Take the `Recommended` option automatically wherever this skill would
otherwise call `AskUserQuestion` for a task-selection, rescoping, or
mechanism-design choice** — a chain that stops to ask about every
judgment call isn't unattended, and "this needs a design decision" is not
by itself a reason to stop (see step 2's "Research and decide"). Note
each automatic choice in your eventual report so a person can revisit any
of them later. Exhaust the current task family (all of AD-NNN.MM's
slices) before moving to AD-NNN.(MM+1) or the next AD-(NNN+1), same as
step 2 says.

This does not apply to the short, genuine list below — these really do
need a person, and nothing about "research and decide" is meant to paper
over them:

- **A task cannot be attempted, at any split depth, without relaxing or
  editing the repo's own gating/verification tooling.** This is the one
  content-level stop that survives no matter how much research or
  splitting you do. Report exactly which gate and why the task's real
  requirement conflicts with it.
- **No eligible task remains anywhere in the project** — not just the
  current phase/family; check adjacent phases and the top-level backlog
  too. This should be rare, since "the next slice needs research" is no
  longer a reason to treat a family as exhausted — only genuinely running
  out of backlog, or hitting the gating-tooling wall on every remaining
  item even after real attempts to split around it, counts.
- **An environment-level block**, most notably a merge/push refused by the
  harness's own permission classifier (step 7) — note that the user has
  already granted standing authorization for Claude to run `gh pr merge`
  as part of this skill, so a classifier refusal here is the separate
  harness-level gate itself firing, not a missing conversational approval;
  it is still outside gnhf's control. Report it (pointing at the
  `update-config` skill / a settings.json Bash permission rule as the
  actual fix) and wait, then resume the chain from where it stopped once
  resolved.
- **The user interrupts or stops the chain directly.**

Every other fork in this skill (which task's next, whether to split a
vague task, which candidate tick amount or stacking policy a launched
run should pick, etc.) has a stated recommended default specifically so
the chain doesn't stall on it — use that default and keep going.

**Named anti-pattern (the actual failure this section exists to prevent):**
"I researched everything left in this family and each one needs a real
subsystem, so let me check with the user before committing to that much
scope" is not caution, it is the chain stopping itself over something
already covered by "Research and decide" (step 2) — see the standing
preference recorded there for the full corrected procedure and the
incident it came from. A task's size, or the fact that the *next* slice
requires inventing a mechanism rather than a value, is never by itself a
reason to pause the chain. If a genuine stop is warranted, it will always
be one of the four bullets above — check the list, don't reason from how
big or unfamiliar the work feels.

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
