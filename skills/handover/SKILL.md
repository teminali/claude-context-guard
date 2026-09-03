---
name: handover
description: End a long session cleanly and hand it to a fresh chat. Writes a lane-tagged handover doc (goal, state, in-flight work, next steps, decisions, landmines) that the next session picks up automatically from a bare "continue" - no prompt to paste. Use when the context guard fires AMBER/RED/CRITICAL, when context is large or the session feels slow or forgetful, when switching to an unrelated task, or when the user says "handover", "hand this over", "context is full", "start a new chat", "save tokens", "wrap up this session".
---

# Handover

Turn an expensive, context-heavy session into a cheap fresh one, losing nothing that matters.

A fresh session that reads a good handover doc starts at roughly 5-10k tokens instead of
inheriting 300-600k. That is the entire point: **the doc must carry the knowledge, not the
transcript.**

## When to run

- **Guard fired AMBER (110k) — this is the cheap moment to go.** The handover itself costs
  ~4 round trips at whatever your context is, so handing over at 110k costs roughly a third
  of what the same handover costs at 280k. Waiting for RED does not save the work, it just
  buys it at a worse price.
- The context guard fired RED or CRITICAL (do it now, before any more tool calls).
- The user is switching to an unrelated task — hand over, then tell them to `/clear`.
- You notice you are re-reading files you already read, or forgetting earlier decisions.

## Procedure

### 1. Stop

Do not start new work. Finish only a half-applied edit that would leave the tree broken.
Say in one line what you are stopping mid-way, if anything.

### 2. Gather the hard facts (ONE command)

```bash
python3 ~/.claude/handover/bin/ctx.py brief
```

One call, deliberately. You are running at peak context, so every extra tool call re-sends
the entire session — two commands here cost more than the doc you are about to write.
`brief` is `status` + `facts` in a single round trip, trimmed to what changes the next
session's behaviour. Add `--full` only if the trimmed form is genuinely missing something.

It reads the transcript directly and gives you: every user prompt in order, files
edited and read, recent commands, the last todo list, git branch + uncommitted files +
recent commits, and tool-call counts. Use it — do not reconstruct this from memory, and do
not re-read project files to write the doc.

### 3. Write the body

Write to the scratchpad, e.g. `$SCRATCH/handover-body.md`. Use exactly these sections.

````markdown
## Goal
One paragraph. What the user actually wants, in their terms. Include the original ask and
how it evolved — from the prompt list in `facts`, not from your summary of it.

## Current state
What is DONE and verified working. Be specific: feature, file, and how it was confirmed
(build passed, screenshot checked, test green). Mark anything done-but-unverified.

## In flight
The exact stopping point. `path/to/file.ts:120` — what was being changed and why it is
not finished. Say "nothing in flight" if the tree is clean.

## Next steps
Ordered, concrete, each one actionable without further discovery. Not "improve the drawer"
but "in components/layout/app-sidebar.tsx, replace the More dialog footer buttons with a
segmented control; the palette tokens are --color-teal-* (re-pointed per business theme)".

## Decisions and constraints
The expensive knowledge — the reason a fresh session would otherwise repeat work. User
preferences stated in chat, rejected approaches, naming conventions, "the client wants X
not Y", API quirks discovered, why a workaround exists. This section is the most valuable
part of the doc.

## Landmines
What NOT to do. Approaches already tried that failed, files that look relevant but are
not, commands that break things, things the user explicitly rejected.

## Files that matter
Path + one line on its role. Paths only — never paste file contents.

## Verification
The exact commands that prove the work is good (build, typecheck, lint, test, dev server).
Include known-failing ones and whether the failures pre-date this work.

## Start-here prompt

```text
Continue work on <project>. Read the handover first: <ABSOLUTE_PATH_TO_DOC>

Short restatement of the goal and the single next action.

Do not re-explore the codebase; the handover lists the files that matter.
```
````

Rules for the body:

- **Never paste file contents, diffs, logs, or command output.** Reference `path:line`.
  A handover that inlines code is a handover that costs as much as the session it replaces.
- Write for someone with zero memory of this chat but full access to the repo.
- Aim for 60-150 lines. If it is longer, you are transcribing instead of summarising.
- Absolute paths, so the new session can open them directly.
- Leave the `## Start-here prompt` heading and its fenced block exactly as shown —
  the tooling extracts that block for the clipboard.

### 4. Finalise

```bash
python3 ~/.claude/handover/bin/ctx.py write --body "$SCRATCH/handover-body.md" \
  --cwd "$(pwd)" --title "short title" --lane <thread-slug> --quiet
```

`--lane` is what makes concurrent sessions safe. It names the *thread of work*, not the
project: `recorder-crash`, `sidebar-redesign`, `stripe-webhooks`. Two sessions open on the
same repo doing unrelated things must use different lanes — a doc only ever supersedes,
and is only ever offered to, its own lane. Omit it and it is derived from the title, which
is fine for a single-session project and wrong the moment there are two.

If this session picked up a handover, it is already bound to that lane and keeps it
automatically; pass `--lane` only to start a genuinely new thread.

This stamps machine, session, branch, model and context size onto the doc, saves it to
`<project>/.claude/handover/HANDOVER-<timestamp>.md`, updates `LATEST.md`, mirrors it to
the shared folder if one is configured (so another computer can pick it up), and copies the
start-here prompt to the clipboard as a fallback — the next session will not need it, since
SessionStart hands itself the doc.

### 5. Hand off, and stop

**You cannot `/clear`.** It is a built-in CLI command, not a tool, and no hook can invoke
one either — the transcript being reset is the one you are running inside. That step is
the user's and always will be, so the rest of the design assumes they will sometimes
forget: docs supersede their predecessors and get consumed on sight, which makes a missed
`/clear` merely expensive rather than something that strands the doc forever.

Say three lines, no more:

1. Where the doc is, and which lane it is on.
2. That the next session picks it up automatically — `continue` is the whole message, and
   there is nothing to paste.
3. That `/clear` (same project) or a new chat window is the next move.

End on a bare `/clear` so it is one keystroke away rather than buried in prose. Then
**stop**. Do not begin new work in this session — a handover that is written and then
worked past is stale, and the next session will act on it as though it were not.

## Picking a handover up

SessionStart injects the pending doc into a fresh session automatically. There is no
start-here prompt to paste and no clipboard step — **a bare `continue` is enough.**

**One lane pending** (the normal case): the doc's path, lane and title are already in your
context and it is already marked consumed. Read it, confirm the next step in one line, and
start. Do not re-explore the codebase; the doc lists the files that matter.

**Several lanes pending** (two sessions were open on this project): nothing is claimed, on
purpose. Resolve it like this, in order:

1. If the user's first message clearly matches one lane, claim it in one call:
   ```bash
   python3 ~/.claude/handover/bin/ctx.py pickup --lane <lane>
   ```
   That prints the whole doc *and* marks it consumed — one round trip, not two.
2. If it is ambiguous — a bare `continue` with several lanes open — **ask which lane**,
   listing the titles. Never pick for them.

Guessing is the one unrecoverable failure mode here. A session with no handover asks a
question; a session with the *wrong* handover confidently executes another thread's plan.

**Nothing pending**: say so plainly and ask what to work on. Do not infer a task from a
stale doc that was already consumed.

Once claimed, the session is pinned to that lane: it will never be offered a sibling
lane's doc on resume, and any handover it writes later stays on the same thread.

### Delegate the expensive half of a pickup

Two subagents exist so that verification output and git archaeology never enter a fresh
main context. A token that lands in main context is re-sent on every remaining turn; a
subagent's reading is paid once. Use them by name with the `Agent` tool.

| Agent | Model | Send it | Get back |
|---|---|---|---|
| `verify-pickup` | haiku | the doc path, after a pickup | `VERDICT: PASS/FAIL` + the first real error, ~10 lines |
| `handover-staleness` | sonnet | the doc path, when the doc is over ~6h old | `N of M already done`, each with a commit or `path:line` |

Rules of thumb:

- Run `verify-pickup` before you start editing, not after — it tells you whether the
  branch is where the doc claims. Skip it only when the doc has no `## Verification`
  block, or when its commands are trivially cheap and you need the output anyway.
- Run `handover-staleness` when the doc is old, when `git status` shows changes the doc
  does not mention, or when `ctx.py list` shows another machine wrote in this project
  since. On a doc minutes old it is pure overhead — skip it.
- Both report; neither fixes. A `DONE` without a commit hash or `path:line` is not a
  `DONE` — re-check that step yourself before skipping it.

## Other commands

| Command | Use |
|---|---|
| `ctx.py brief` | **status + facts in one call** — use this when writing a handover |
| `ctx.py pickup --lane X` | print and claim a lane's doc in one call (exit 2 = ambiguous) |
| `ctx.py status` | context tokens, band, thresholds for this session |
| `ctx.py report --days 7` | where tokens actually went, across all local sessions |
| `ctx.py savings` | what handing over right now would save from here |
| `ctx.py savings --all` | what the handovers already written actually saved |
| `ctx.py list` | handovers for this project, with lanes, from every machine |
| `ctx.py show` | print the newest handover |
| `ctx.py doctor` | verify hooks, statusline, config, share dir |
| `ctx.py install` | wire the guard into settings.json (run once per machine) |
