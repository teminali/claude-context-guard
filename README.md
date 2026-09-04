# Context guard + handover

Stops Claude Code sessions from silently growing into 300-600k-token monsters, and
hands the work to a fresh chat without losing anything.

Pure stdlib Python, one file, no dependencies.

## Why

Context is re-sent on every turn. A 300k-token session does not cost 300k once — it
costs 300k *per remaining turn*. `ctx.py report` on one machine, over three days:

```
tokens re-sent as context : 1,514.9M
turns above 150k context  : 71%
avg context per turn      : 314k
```

Every one of those turns paid for the whole conversation again. The guard makes that
visible while it is happening, and the handover makes stopping cheap.

## Install

```bash
git clone https://github.com/teminali/claude-context-guard
cd claude-context-guard && ./install.sh
```

Idempotent — re-run it after any `git pull`. It never overwrites your `config.json`,
your handover docs, or your per-session state. Open `/hooks` once (or restart Claude
Code) so the hooks load.

You only do that once: from then on it [keeps itself up to date](#staying-up-to-date).

## What gets installed

| Piece | Where | Does |
|---|---|---|
| `bin/ctx.py` | `~/.claude/handover/` | the whole engine, stdlib-only |
| `config.json` | `~/.claude/handover/` | thresholds, share folder, toggles |
| `state/` | `~/.claude/handover/` | per-session band state (which warnings already fired) |
| `backup/` | `~/.claude/handover/` | the last 3 versions it replaced, for `update --rollback` |
| guard hooks | `~/.claude/settings.json` | PostToolUse + UserPromptSubmit + SessionStart |
| status line | `~/.claude/settings.json` | live `ctx 171k/220k [######..]` readout |
| `handover` skill | `~/.claude/skills/handover/` | `/handover` — writes the doc |
| 2 subagents | `~/.claude/agents/` | verify and staleness-check a pickup off-context |

## Bands

Absolute token counts, because cost tracks absolute context, not percentage of window.

| Band | Default | Behaviour |
|---|---|---|
| AMBER | 110k | "finish this step, open nothing new" |
| RED | 160k | "stop new work, run /handover now" |
| CRITICAL | 220k | blocks the tool call and forces the handover |

Each band fires once per session, and re-arms if context drops (after `/clear` or a
compaction). Edit `config.json` to retune; `enabled: false` turns the whole thing off.

The window is only *known* once a session passes 200k (transcripts record
`claude-opus-5` for both the 200k and 1M variants), so percentage trips apply only when
it is known. Pin it with `assume_window` if you always use one model — the bands
themselves are absolute, so pinning changes the readout, not when they fire.

Every display is drawn against CRITICAL rather than the window, because "12% of a 1M
window" and "past AMBER, wrap up" are both true at 123k and only one of them is useful.

## Lanes: two sessions, one repo

The project is not a fine enough key. Two sessions can be open on the same repo doing
unrelated things, and handing a session the *wrong* doc is worse than handing it none —
it executes another thread's plan with full confidence.

So every handover is tagged with a **lane**: one thread of work. On a fresh session:

- **One lane pending** — the doc is injected and claimed in the same breath. A bare
  `continue` picks up exactly where the last session stopped. Nothing to paste.
- **Several lanes pending** — the list is injected and *nothing* is claimed. Claude asks
  which one. Guessing is the one unrecoverable failure, so it does not guess.
- **A session already bound to a lane** is never offered a sibling lane's doc.

Writing a handover retires only its own lane's previous doc, so concurrent sessions
cannot silently kill each other's work.

### The claim is exclusive

A picked-up handover is owned. `status:` in the doc's own frontmatter is the lock —
the one thing every session and every machine can see — and it carries the holder's
machine and session id.

- Claiming a doc someone else holds **fails with exit 3** and prints who has it. It was
  previously a courtesy: the rewrite quietly did nothing on an already-claimed doc and
  the caller carried on as though it had won, so two agents could work one lane while
  each believed it owned it.
- `pickup` that loses the race does **not** print the doc. Printing it *is* the pickup.
- Claiming twice from the same session is a no-op, not a refusal — a resumed session
  must be able to re-run its own pickup.
- The read-modify-write is guarded by an `O_EXCL` sidecar lock, so two sessions starting
  in the same second cannot both see `pending`. A lock older than 90s is treated as a
  killed process and broken.
- A session that claims a lane and then dies would otherwise take the work with it, since
  claimed docs are filtered out of every offer. `ctx.py release --lane X` hands it back.
  There is no timeout: nothing here can tell a crash from an agent that is still
  thinking, so releasing is deliberate.

## Picking up cheaply

Two subagents exist so that a fresh session does not pay, in its own context, for
reading it will never need again. A main-context token is re-sent every turn; a
subagent's reading is paid once.

| Agent | Model | Returns |
|---|---|---|
| `verify-pickup` | haiku | runs the doc's `## Verification` block → `VERDICT` + the first real error, ~10 lines |
| `handover-staleness` | sonnet | diffs `## Next steps` against git → `N of M already done`, each with a commit or `path:line` |

Measured on this repo's own handover: 25.7k tokens spent inside the two agents, about
20 lines returned. Both report; neither fixes. A `DONE` without a citation is not a
`DONE`.

## Commands

```bash
ctx=~/.claude/handover/bin/ctx.py
python3 $ctx status              # context size and band for this session
python3 $ctx brief               # status + facts in one call (use when writing a doc)
python3 $ctx pickup --lane X     # print a lane's doc and claim it, one call
python3 $ctx report --days 7     # where tokens actually went, all local sessions
python3 $ctx savings             # what handing over right now would save
python3 $ctx savings --all       # what the handovers already written actually saved
python3 $ctx list                # open handovers for this project, with lanes
python3 $ctx list --all          # ...including claimed ones, and who holds them
python3 $ctx show                # print the newest one
python3 $ctx consume <file>      # claim one (exit 3 = another session holds it)
python3 $ctx release --lane X    # hand a claim back after a session dies
python3 $ctx doctor              # verify the install
python3 $ctx update --check      # is a newer version published?
python3 $ctx update              # install it now, without waiting for the daily check
python3 $ctx update --rollback   # put the previous version back, and stay there
```

## Staying up to date

The tool is *copied* into `~/.claude`, not symlinked from a checkout, so a fix published
here used to reach a machine only when somebody remembered to `git pull && ./install.sh`.
Nobody remembers. So it fetches its own updates:

- **Once every 24 hours**, on the first prompt of any session after the interval is up.
- **In a detached process.** A hook has a ~10s budget and shares it with your turn;
  nothing on that path ever touches the network. The check is one small file read, and
  at most once a day, one `Popen`.
- **Validated before it lands.** Every file is fetched and checked first — the new
  `ctx.py` must parse (`compile()`) and look like a whole file — and only then are the
  installed copies replaced, atomically, with `os.replace`. A half-applied update would
  break the very code that has to explain itself.
- **Backed up.** The version it replaced goes to `backup/<version>/`; `update --rollback`
  restores it and pins you there, so the next day's check does not cheerfully reinstall
  what you just backed out of. `update --force` un-pins it.
- **Announced.** The next prompt in *every* open session gets one line: what version
  landed and what changed. Once per session, never twice.
- **Hook-aware.** If a release adds or renames a hook, it re-wires `settings.json` —
  and only then, so releases do not litter your `~/.claude` with `.bak-` files.

A release says what it changed in one line (`UPDATE_NOTE` at the top of `bin/ctx.py`),
and that line is what your sessions are shown.

### Publishing one (maintainer)

```bash
./release.sh 1.6.0 "what changed, in one line"
```

Refuses a version that is not numerically newer than the one on `main` (clients compare
`1.10.0 > 1.9.0` numerically, so a string bump would just be ignored), refuses a dirty
tree, then bumps `VERSION` + `UPDATE_NOTE`, writes the [CHANGELOG](CHANGELOG.md) entry,
commits, tags `v1.6.0` and pushes. Every install picks it up on its next daily check —
that one-line note is the whole message those users get, so write it for them.

Tune or disable it in `config.json`:

```jsonc
"update": {
  "enabled": true,          // false: never check, never phone home
  "auto_apply": true,       // false: tell me a version is out, let me run it
  "check_every_hours": 24,
  "notify": true,           // false: update silently
  "repo": "teminali/claude-context-guard",
  "branch": "main",
  "source": "",             // a fork, a mirror, or file:///... in a test
  "timeout_seconds": 10
}
```

## Does it work?

`savings --all` credits a handover only when a later session actually named its doc in
its first few turns, and credits it once. On the machine this was built on, across 47
handovers with 33 credited pickups:

```
746.9M tokens not re-sent
```

It is an estimate, and worth understanding before you quote it. Each credited row is
`(context at handover - the fresh session's baseline) x the fresh session's turns`. That
holds the abandoned session flat at its handover size, which understates it, and ignores
that the fresh session also grows, which overstates it. The two errors largely cancel, so
read it as an approximation rather than a floor. On a subscription the currency is your
usage allowance rather than dollars.

## Another computer

A hook only runs inside its own session, so nothing here can watch a chat on a
different machine. What travels is the toolkit and the handover docs:

```bash
git clone https://github.com/teminali/claude-context-guard && cd claude-context-guard && ./install.sh
```

`install.sh` merges the hooks into that machine's `~/.claude/settings.json` (with a
backup) and points it at a shared iCloud Drive folder if one exists. Handovers are
written to the project *and* mirrored to `<share_dir>/<project>/`, tagged with the
machine that wrote them — so machine B's SessionStart hook offers the handover machine A
wrote. Any synced folder works: set `share_dir` in `config.json` (Dropbox, a git repo,
an SMB share).

For a repo your team or cloud sessions share, install project-scoped hooks instead:

```bash
python3 ~/.claude/handover/bin/ctx.py install --project /path/to/repo
```

That writes `<repo>/.claude/settings.json`, so anyone working in the repo gets the guard.

## Privacy

Everything stays on your machine. `state/` (session ids, token counts, local doc paths)
and your live `config.json` are gitignored; handover docs are written into your own
projects and, if you configure one, your own synced folder.

Nothing of yours is sent anywhere. The one outbound request is the update check: an
unauthenticated `GET` to `raw.githubusercontent.com`, at most once a day, carrying
nothing but a `ctx.py/<version>` user agent. `"update": {"enabled": false}` stops even
that.

## Turning it off

```bash
python3 -c "import json,pathlib;p=pathlib.Path.home()/'.claude/handover/config.json';c=json.loads(p.read_text());c['enabled']=False;p.write_text(json.dumps(c,indent=2))"
```

Or restore a settings backup: `~/.claude/settings.json.bak-*`.

To keep the guard but stop the updates, set `"update": {"enabled": false}` in
`config.json` — or `"auto_apply": false` to be told about new versions without
installing them.

## License

MIT

## Support

If this saved you time, [a coffee's worth of crypto](DONATE.md) is a good way to say so. It stays free either way.
