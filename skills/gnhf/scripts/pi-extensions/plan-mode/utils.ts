/**
 * Pure utility functions for plan mode.
 * Extracted for testability.
 */

// Destructive commands blocked in plan mode
const DESTRUCTIVE_PATTERNS = [
	/\brm\b/i,
	/\brmdir\b/i,
	/\bmv\b/i,
	/\bcp\b/i,
	/\bmkdir\b/i,
	/\btouch\b/i,
	/\bchmod\b/i,
	/\bchown\b/i,
	/\bchgrp\b/i,
	/\bln\b/i,
	/\btee\b/i,
	/\btruncate\b/i,
	// `dd` requires one of its own argument flags (`if=`/`of=`/`bs=`/etc.)
	// rather than matching the bare two letters anywhere -- found live
	// (AD-003.09.09, 2026-09-12): a python heredoc computing overlap
	// windows used a plain local variable named `dd`, which the old
	// unconditional `\bdd\b` misread as an invocation of the disk-copy
	// command. A real `dd` command is never usefully invoked without at
	// least one of these arguments, so requiring one costs nothing.
	/\bdd\s+(if|of|bs|count|conv|skip|seek)=/i,
	/\bshred\b/i,
	// Redirect detection deliberately exempts stderr-to-null/fd-duplication
	// idioms (`2>/dev/null`, `2>&1`, `&>/dev/null`, `>>/dev/null`) -- these
	// discard output or merge streams, they never write to a real file, so
	// flagging them as destructive is a false positive, not caution. Found
	// live (AD-003.09.04, 2026-09-12): almost every practical multi-command
	// bash invocation includes a `2>/dev/null` to suppress noise, so the
	// unconditional version blocked 119 of 137 tool calls (87%) on one real
	// task before any file was ever touched -- not an edge case, the common
	// path. A redirect to anything else (a real path) still matches and
	// stays blocked; only these three specific safe forms are exempted.
	// Also exempts `>=` (a numeric/string comparison, never a redirect
	// operator in any shell) and a bare `>` immediately followed by a
	// number then whitespace/EOL/closing quote-paren-brace-bracket-semicolon
	// -- e.g. `NR>=436`, `j >= 0`, `length($0) > 100)`, `$1>5{print}`. Found
	// live (AD-003.09.06, 2026-09-12): every awk one-liner or inline
	// python snippet that compares a line number/length against a bound
	// contains exactly this shape, and none of it writes to a file --
	// a real destructive redirect targets a path, which is never a bare
	// integer immediately closed off like that. A redirect to a real path
	// (`> output.txt`, `> 5file.txt`, `>> log`) still matches and stays
	// blocked. The preceding-character exclusion also now excludes `-`,
	// so `->` (Python type-hint/docstring/prose arrow notation, e.g.
	// `def f() -> int:` or "removed -> replaced with") no longer matches
	// either -- found live (AD-003.09.09, 2026-09-12) in plain English
	// task-note prose passed through a python heredoc. No real shell
	// redirect syntax is ever written with a literal `-` immediately
	// before the `>` (that position is always a digit for an fd number,
	// as in `2>`, or nothing), so this loses no real coverage.
	/(^|[^<-])>(?!>)(?!&)(?!=)(?!\s*\/dev\/null\b)(?!\s*-?\d+(?:\s|$|["')}\]{;]))/,
	/>>(?!\s*\/dev\/null\b)/,
	/\bnpm\s+(install|uninstall|update|ci|link|publish)/i,
	/\byarn\s+(add|remove|install|publish)/i,
	/\bpnpm\s+(add|remove|install|publish)/i,
	/\bpip\s+(install|uninstall)/i,
	/\bapt(-get)?\s+(install|remove|purge|update|upgrade)/i,
	/\bbrew\s+(install|uninstall|upgrade)/i,
	/\bgit\s+(add|commit|push|pull|merge|rebase|reset|checkout|branch\s+-[dD]|stash|cherry-pick|revert|tag|init|clone)/i,
	/\bsudo\b/i,
	/\bsu\b/i,
	/\bkill\b/i,
	/\bpkill\b/i,
	/\bkillall\b/i,
	/\breboot\b/i,
	/\bshutdown\b/i,
	/\bsystemctl\s+(start|stop|restart|enable|disable)/i,
	/\bservice\s+\S+\s+(start|stop|restart)/i,
	// Anchored to command position (start of string, or right after a
	// command separator) rather than matching anywhere in the string --
	// found live (AD-003.09.09, 2026-09-12): "code" is an ordinary
	// English word that appears constantly in comments, docstrings, and
	// review prose ("the code(lines starting...", "print(code)"), and
	// the old unconditional `\bcode\b` misread every one of those as an
	// attempt to launch the VS Code CLI. `vim`/`nano`/`emacs`/`subl` are
	// far less likely to collide with ordinary prose, but are anchored
	// the same way for consistency -- a real editor invocation is always
	// the command (or one piped/chained command) being run, never a
	// substring inside a larger word or quoted text.
	/(^|[;&|]\s*)(vim?|nano|emacs|code|subl)\b/i,
];

// Safe read-only commands allowed in plan mode
const SAFE_PATTERNS = [
	// `cd` alone has no destructive potential -- it only changes the shell's
	// own working directory, never writes or executes anything else -- so
	// allowlisting it as a start-pattern costs nothing: DESTRUCTIVE_PATTERNS
	// scans the whole command string, not just what follows `cd`, so
	// `cd X && rm Y` still fails on the `rm` match regardless of this entry.
	// Missing this, found live (AD-003.09.05, 2026-09-12): every command a
	// model in this repo's own worktree convention writes starts with
	// `cd <worktree-path> && <otherwise-safe command>` (matching how every
	// gnhf task prompt is framed), and none of those matched any safe
	// pattern at all, so they were rejected regardless of what followed.
	/^\s*cd\b/,
	/^\s*cat\b/,
	/^\s*head\b/,
	/^\s*tail\b/,
	/^\s*less\b/,
	/^\s*more\b/,
	/^\s*grep\b/,
	/^\s*find\b/,
	/^\s*ls\b/,
	/^\s*pwd\b/,
	/^\s*echo\b/,
	/^\s*printf\b/,
	/^\s*wc\b/,
	/^\s*sort\b/,
	/^\s*uniq\b/,
	/^\s*diff\b/,
	/^\s*file\b/,
	/^\s*stat\b/,
	/^\s*du\b/,
	/^\s*df\b/,
	/^\s*tree\b/,
	/^\s*which\b/,
	/^\s*whereis\b/,
	/^\s*type\b/,
	/^\s*env\b/,
	/^\s*printenv\b/,
	/^\s*uname\b/,
	/^\s*whoami\b/,
	/^\s*id\b/,
	/^\s*date\b/,
	/^\s*cal\b/,
	/^\s*uptime\b/,
	/^\s*ps\b/,
	/^\s*top\b/,
	/^\s*htop\b/,
	/^\s*free\b/,
	/^\s*git\s+(status|log|diff|show|branch|remote|config\s+--get)/i,
	/^\s*git\s+ls-/i,
	/^\s*npm\s+(list|ls|view|info|search|outdated|audit)/i,
	/^\s*yarn\s+(list|info|why|audit)/i,
	/^\s*node\s+--version/i,
	/^\s*python\s+--version/i,
	/^\s*curl\s/i,
	/^\s*wget\s+-O\s*-/i,
	/^\s*jq\b/,
	/^\s*sed\s+-n/i,
	/^\s*awk\b/,
	/^\s*rg\b/,
	/^\s*fd\b/,
	/^\s*bat\b/,
	/^\s*eza\b/,
];

export function isSafeCommand(command: string): boolean {
	const isDestructive = DESTRUCTIVE_PATTERNS.some((p) => p.test(command));
	const isSafe = SAFE_PATTERNS.some((p) => p.test(command));
	return !isDestructive && isSafe;
}

export interface TodoItem {
	step: number;
	text: string;
	completed: boolean;
}

export function cleanStepText(text: string): string {
	let cleaned = text
		.replace(/\*{1,2}([^*]+)\*{1,2}/g, "$1") // Remove bold/italic
		.replace(/`([^`]+)`/g, "$1") // Remove code
		.replace(
			/^(Use|Run|Execute|Create|Write|Read|Check|Verify|Update|Modify|Add|Remove|Delete|Install)\s+(the\s+)?/i,
			"",
		)
		.replace(/\s+/g, " ")
		.trim();

	if (cleaned.length > 0) {
		cleaned = cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
	}
	if (cleaned.length > 50) {
		cleaned = `${cleaned.slice(0, 47)}...`;
	}
	return cleaned;
}

export function extractTodoItems(message: string): TodoItem[] {
	const items: TodoItem[] = [];
	const headerMatch = message.match(/\*{0,2}Plan:\*{0,2}\s*\n/i);
	if (!headerMatch) return items;

	const planSection = message.slice(message.indexOf(headerMatch[0]) + headerMatch[0].length);
	const numberedPattern = /^\s*(\d+)[.)]\s+\*{0,2}([^*\n]+)/gm;

	for (const match of planSection.matchAll(numberedPattern)) {
		const text = match[2]
			.trim()
			.replace(/\*{1,2}$/, "")
			.trim();
		if (text.length > 5 && !text.startsWith("`") && !text.startsWith("/") && !text.startsWith("-")) {
			const cleaned = cleanStepText(text);
			if (cleaned.length > 3) {
				items.push({ step: items.length + 1, text: cleaned, completed: false });
			}
		}
	}
	return items;
}

export function extractDoneSteps(message: string): number[] {
	const steps: number[] = [];
	for (const match of message.matchAll(/\[DONE:(\d+)\]/gi)) {
		const step = Number(match[1]);
		if (Number.isFinite(step)) steps.push(step);
	}
	return steps;
}

export function markCompletedSteps(text: string, items: TodoItem[]): number {
	const doneSteps = extractDoneSteps(text);
	for (const step of doneSteps) {
		const item = items.find((t) => t.step === step);
		if (item) item.completed = true;
	}
	return doneSteps.length;
}
