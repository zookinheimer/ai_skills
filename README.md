# ai_skills

Personal collection of [Agent Skills](https://agentskills.io/specification)
— self-contained `SKILL.md` capability packages that Claude Code, opencode,
pi, and other compatible agents load on demand. Each skill lives in its own
directory under `skills/`.

This is [zookinheimer](https://github.com/zookinheimer)'s fork of
[pythoninthegrass/ai_skills](https://github.com/pythoninthegrass/ai_skills).
On top of upstream, `gnhf` drives its unattended runs through opencode's
own server API by default — a real interactive TUI stays attached and
watchable (in a Herdr pane, when available) while a session-scoped
permission ruleset and polling loop handle everything unattended.
`pi` remains supported as a secondary agent. See
[skills/gnhf/scripts/gnhf.py](skills/gnhf/scripts/gnhf.py) and
[skills/gnhf/SKILL.md](skills/gnhf/SKILL.md).

## Skills

| Skill  | Description |
| ------ | ----------- |
| [gnhf](skills/gnhf/SKILL.md) | Launch a bounded, low-supervision overnight coding agent run against one well-specced task, in an isolated worktree. |

## Install

The [`skills` CLI](https://github.com/vercel-labs/skills) is the easiest
path — it detects installed agents and symlinks the skill into each one:

```bash
npx skills add zookinheimer/ai_skills
```

Useful flags: `-g` installs user-level instead of project-level, `-a
claude-code,opencode,pi` targets specific agents, `-s gnhf` installs one
skill by name, `-y` skips confirmation prompts, `--copy` copies files
instead of symlinking. `npx skills list`, `npx skills update`, and `npx
skills remove` manage what's installed.

To try a skill without installing it:

```bash
npx skills use zookinheimer/ai_skills@gnhf | claude
```

### Update

```bash
npx skills update
```

Checks every installed skill's lock-file hash against its source and
refreshes only the ones that changed upstream, reporting `N skills already
up to date` otherwise. This is the intended way to pull in new commits from
this repo — `npx skills add` also overwrites an already-installed skill
(flagged as `overwrites:` in its install summary) if you re-run it, but it
re-runs the full interactive install flow each time rather than just
refreshing what changed.

If you installed manually (below), `git pull` in your checkout is enough —
the symlink always points at the working tree.

### Manual install

`skills add` installs a pinned snapshot (refreshed via `npx skills update`),
not a live link to a working checkout. For local development, or on a
machine without npx, clone the repo and symlink directly — pi and opencode
both auto-load `~/.agents/skills/`, and opencode also auto-loads
`~/.claude/skills/`, so two symlinks cover all three agents:

```bash
git clone https://github.com/zookinheimer/ai_skills.git ~/git/ai_skills
mkdir -p ~/.agents/skills ~/.claude/skills
ln -s ~/git/ai_skills/skills/gnhf ~/.agents/skills/gnhf   # pi, opencode
ln -s ~/git/ai_skills/skills/gnhf ~/.claude/skills/gnhf   # Claude Code
```

Documented discovery locations, per agent (see each agent's own docs for
the current, authoritative list — these locations are more stable than any
CLI flags):

| Agent | Personal | Project | Declare extra dirs |
| ----- | -------- | ------- | ------------------- |
| [Claude Code](https://docs.claude.com/en/docs/claude-code) | `~/.claude/skills/<name>/` | `.claude/skills/<name>/` | — |
| [opencode](https://opencode.ai/docs) | `~/.config/opencode/skill(s)/<name>/` | `.opencode/skill(s)/<name>/` | `skills.paths` in `opencode.json` |
| [pi](https://github.com/badlogic/pi-mono) | `~/.pi/agent/skills/`, `~/.agents/skills/` | `.pi/skills/`, `.agents/skills/` | `skills` array in `~/.pi/agent/settings.json`, or `pi --skill <path>` |

## Verify it loaded

Skills are scanned at startup — restart the agent (or start a new session)
after installing. Then:

- Claude Code: run `/gnhf`
- pi: run `/skill:gnhf`
- opencode: mention what you want done; opencode picks the skill up from
  its description
- Either way: `npx skills list -g` shows what the CLI has installed

## Repo layout

```text
skills/
└── gnhf/
    ├── SKILL.md
    └── scripts/
        ├── gnhf.py                     # main unattended agent driver
        ├── test_gnhf.py                # test suite
        └── mcp-defaults.json           # MCP server configuration
```

To add a new skill, create a directory under `skills/` with a `SKILL.md`
carrying `name` and `description` frontmatter (`npx skills init <name>`
scaffolds one), then install/symlink it the same way as above.

## Security

A skill's `SKILL.md` is instructions the model reads and follows, and any
bundled scripts are code the model can execute. Review both before
installing a skill from anywhere, including this repo. `gnhf` bundles
`scripts/gnhf.py`, `scripts/test_gnhf.py`, and configuration in
`scripts/mcp-defaults.json`.

## Prior art

- [kunchenguid/gnhf](https://github.com/kunchenguid/gnhf) — an unrelated npm
  package that shares this skill's name. This `gnhf` skill was written from
  scratch for how this author runs unattended agents (manual git worktree,
  a plain background process, `timeout` for the wall-clock bound), but that
  project's README was useful reference material while naming and scoping
  this one.
