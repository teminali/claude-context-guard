#!/usr/bin/env python3
"""
ctx.py - Claude Code context guard + session handover toolkit.

Portable: copy the whole ~/.claude/handover folder to any machine,
run `python3 bin/ctx.py install`, and that machine gets the same behaviour.

Subcommands:
  guard         hook entry (PostToolUse / UserPromptSubmit) - stdin JSON -> hook JSON
  sessionstart  hook entry (SessionStart) - surfaces a pending handover
  statusline    statusLine command - stdin JSON -> one status line
  status        human-readable context reading for the current session
  facts         extract hard facts from a transcript for a handover doc
  write         finalise a handover doc (adds metadata, mirrors, clipboard)
  list          list recent handovers (all machines, if a share dir is set)
  show          print a handover doc
  consume       mark a handover as picked up
  savings       what a handover here would save; --all for what past ones did
  report        token-waste report across local transcripts
  install       merge hooks + statusLine into a settings.json
  doctor        verify the install
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.3.0"
HOME = Path.home()
ROOT = Path(os.environ.get("CLAUDE_HANDOVER_ROOT", str(HOME / ".claude" / "handover")))
STATE_DIR = ROOT / "state"
CONFIG_PATH = ROOT / "config.json"
PROJECTS = HOME / ".claude" / "projects"
MACHINE = socket.gethostname().split(".")[0]

DEFAULT_CONFIG = {
    "enabled": True,
    # absolute context-token thresholds - cost scales with absolute context,
    # not with percentage of the window
    "thresholds": {"amber": 110000, "red": 160000, "critical": 220000},
    # ...but also trip relative to a small window (200k models)
    "pct_of_window": {"red": 0.70, "critical": 0.85},
    "block_at_critical": True,
    "block_at_red": False,       # RED nags hard but does not block by default
    # a single nudge per band is easy to scroll past - re-warn every N tokens of
    # further growth inside the same band
    "renotify_tokens": 25000,
    "big_tool_result_tokens": 25000,
    "min_growth_bytes": 40000,   # skip re-reading transcript until it grows this much
    "share_dir": "",             # e.g. ~/Library/Mobile Documents/com~apple~CloudDocs/claude-handovers
    "extra_transcript_dirs": [],
    "clipboard": True,
    # Mark a handover consumed the moment SessionStart offers it, rather than
    # waiting for the agent to run `consume` by hand. Set false to go back to
    # an explicit pickup.
    "auto_consume": True,
    # How stale an unclaimed handover may be and still be offered at SessionStart.
    "offer_max_age_hours": 48,
    "assume_window": 0,   # 0 = auto-detect; pin to 1000000 or 200000 to be explicit
    "projection_turns": 20,   # turns the session would plausibly have continued
    "pricing": {
        # USD per 1M tokens, Anthropic list price. Verify at anthropic.com/pricing.
        # Cache read is 0.1x input; a 5m cache write is 1.25x input.
        "cache_read_multiplier": 0.1,
        "cache_write_multiplier": 1.25,
        "models": {
            "claude-opus-5":    {"input": 5.0,  "output": 25.0},
            "claude-opus-4-8":  {"input": 5.0,  "output": 25.0},
            "claude-fable-5":   {"input": 10.0, "output": 50.0},
            "claude-sonnet-5":  {"input": 2.0,  "output": 10.0},
            "claude-haiku-4-5": {"input": 1.0,  "output": 5.0},
        },
        "fallback": {"input": 5.0, "output": 25.0},
    },
}

BANDS = ["green", "amber", "red", "critical"]


# ----------------------------------------------------------------- config ---
def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        user = json.loads(CONFIG_PATH.read_text())
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


# ------------------------------------------------------------- transcript ---
def tail_bytes(path: Path, nbytes: int) -> list[bytes]:
    size = path.stat().st_size
    start = max(0, size - nbytes)
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read()
    if start > 0:
        i = data.find(b"\n")
        data = data[i + 1:] if i >= 0 else b""
    return data.split(b"\n")


def read_context(path: Path) -> dict | None:
    """Latest main-thread context size, from the newest assistant usage record."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    for nb in (262144, 1048576, 4194304, 16777216, 67108864):
        for raw in reversed(tail_bytes(path, nb)):
            if b'"usage"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except Exception:
                continue
            if d.get("isSidechain"):
                continue
            msg = d.get("message") or {}
            u = msg.get("usage") or {}
            tok = ((u.get("input_tokens") or 0)
                   + (u.get("cache_read_input_tokens") or 0)
                   + (u.get("cache_creation_input_tokens") or 0))
            if tok <= 0:
                continue
            return {
                "tokens": tok,
                "model": msg.get("model") or "",
                "ts": d.get("timestamp") or "",
                "cwd": d.get("cwd") or "",
                "session_id": d.get("sessionId") or "",
                "git_branch": d.get("gitBranch") or "",
            }
        if nb >= size:
            break
    return None


def window_for(model: str, observed: int, cfg: dict | None = None) -> int:
    return window_detail(model, observed, cfg)[0]


def window_detail(model: str, observed: int, cfg: dict | None = None) -> tuple[int, bool]:
    """(window, known). Transcripts record `claude-opus-5` even for the 1M variant, so
    the window is only *known* once context has passed 200k or it is pinned in config."""
    env = os.environ.get("CLAUDE_CTX_WINDOW")
    if env and env.isdigit():
        return int(env), True
    pinned = (cfg or {}).get("assume_window") or 0
    if pinned:
        return int(pinned), True
    if "[1m]" in model or observed > 200000:
        return 1000000, True
    return 200000, False


def band_for(tokens: int, window: int, cfg: dict, window_known: bool = True) -> str:
    """Absolute thresholds lead; the percentage trip only applies to a window we
    actually know, so an unidentified 1M session is never falsely escalated."""
    t = cfg["thresholds"]
    p = cfg["pct_of_window"]
    if window_known:
        crit = min(t["critical"], int(window * p["critical"]))
        red = min(t["red"], int(window * p["red"]))
    else:
        crit, red = t["critical"], t["red"]
    amber = min(t["amber"], red - 1)
    if tokens >= crit:
        return "critical"
    if tokens >= red:
        return "red"
    if tokens >= amber:
        return "amber"
    return "green"


def find_transcript(cwd: str | None, session_id: str | None) -> Path | None:
    """Resolve a transcript. The running session's own id is exported by Claude Code,
    which is the only reliable answer when several sessions share a project dir."""
    cands: list[Path] = []
    ids = [session_id,
           os.environ.get("CLAUDE_CODE_SESSION_ID"),
           os.environ.get("CLAUDE_SESSION_ID")]
    for sid in [i for i in ids if i]:
        for d in PROJECTS.glob("*"):
            p = d / f"{sid}.jsonl"
            if p.exists():
                return p
    if cwd:
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
        d = PROJECTS / slug
        if d.is_dir():
            cands = list(d.glob("*.jsonl"))
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)


def transcript_owns_cwd(tpath: "Path | None", info: dict, cwd: str) -> bool:
    """Does this transcript actually belong to `cwd`?

    find_transcript() answers with the *running* session whenever Claude Code
    exports its id, whatever directory was asked about. That is right for the
    live session and wrong for `write --cwd <other dir>`: the calling session's
    tokens, branch, lane and state file would be stamped onto another project's
    handover. Ownership is filing location first, recorded cwd second - a
    session started in a subdirectory still owns the repo above it.
    """
    if not tpath:
        return False
    try:
        if tpath.parent == PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", str(cwd)):
            return True
    except Exception:
        pass
    tcwd = (info or {}).get("cwd") or ""
    if not tcwd:
        return False
    try:
        a, b = Path(tcwd).resolve(), Path(cwd).resolve()
    except Exception:
        return False
    # Only one direction is safe. A session sitting *inside* the target owns it
    # (a shell that wandered into a subdirectory is still this repo's session).
    # The reverse is how the original bug looked: a session parked in /private/tmp
    # or $HOME would claim every fixture underneath it.
    return a == b or b in a.parents


# ------------------------------------------------------------------ state ---
def state_path(session_id: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id or "unknown")
    return STATE_DIR / f"{safe}.json"


def load_state(session_id: str) -> dict:
    try:
        return json.loads(state_path(session_id).read_text())
    except Exception:
        return {}


def save_state(session_id: str, st: dict) -> None:
    try:
        state_path(session_id).write_text(json.dumps(st))
    except Exception:
        pass


# ------------------------------------------------------------------ utils ---
def fmt_tok(n: int) -> str:
    if n >= 1000000:
        return f"{n / 1000000:.2f}M"
    if n >= 1000:
        return f"{n / 1000:.0f}k"
    return str(n)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")


def project_slug(cwd: str) -> str:
    return Path(cwd).name or "root"


def lane_slug(text: str) -> str:
    """A stable short id for one thread of work inside a project.

    Two sessions can be open on the same repo doing unrelated things. The
    project is not a fine enough key to tell their handovers apart, and handing
    a session the wrong doc is worse than handing it none - it acts on someone
    else's plan with full confidence. The lane is that missing key.
    """
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40].strip("-") or "main"


def session_lane(session_id: str) -> str:
    """The lane this session already committed to, if any."""
    if not session_id:
        return ""
    try:
        return load_state(session_id).get("lane", "") or ""
    except Exception:
        return ""


def bind_session_lane(session_id: str, lane: str) -> None:
    """Pin a session to a lane, so it is never offered another one and any
    handover it writes later stays on the same thread of work."""
    if not session_id or not lane:
        return
    try:
        st = load_state(session_id)
        if st.get("lane") != lane:
            st["lane"] = lane
            save_state(session_id, st)
    except Exception:
        pass


def handover_dir(cwd: str) -> Path:
    d = Path(cwd) / ".claude" / "handover"
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        d = HOME / ".claude" / "handover" / "docs" / project_slug(cwd)
        d.mkdir(parents=True, exist_ok=True)
        return d


def share_dir(cfg: dict) -> Path | None:
    s = (cfg.get("share_dir") or "").strip()
    if not s:
        return None
    p = Path(os.path.expanduser(s))
    try:
        p.mkdir(parents=True, exist_ok=True)
        return p
    except Exception:
        return None


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


# ------------------------------------------------------------------ guard ---
GUARD_TEXT = {
    "amber": (
        "CONTEXT GUARD - AMBER ({tok} tokens in context, ~{cost}x the cost of a fresh turn).{since}\n"
        "Finish the step you are on. Do not open new workstreams, do not spawn subagents, "
        "and prefer targeted `grep`/`sed -n` over reading whole files. "
        "If a new task is coming, say so and offer to run the `handover` skill first.{drift}"
    ),
    "red": (
        "CONTEXT GUARD - RED ({tok} tokens in context). Every further turn re-sends all of it.{since}\n"
        "STOP starting new work now. Complete only the edit in flight, then invoke the "
        "`handover` skill (Skill tool, skill: \"handover\"). It writes a handover doc plus a "
        "paste-ready prompt so a fresh session can continue at a fraction of the cost. "
        "Do not read more files, run broad searches, or spawn subagents before that.{drift}"
    ),
    "critical": (
        "CONTEXT GUARD - CRITICAL ({tok} tokens in context). This turn is expensive and quality degrades.{since}\n"
        "HARD STOP on new work. Do not run further exploratory tools. Immediately invoke the "
        "`handover` skill (Skill tool, skill: \"handover\") to write the handover doc and the "
        "new-chat prompt, tell the user to start a fresh session, and end the turn.{drift}"
    ),
}

# Shown once a handover doc exists for this session: keep nagging, stop blocking,
# so complying with the guard cannot wedge the session.
DONE_TEXT = (
    "CONTEXT GUARD - a handover for this session is already written ({doc}), and context has "
    "grown to {tok} tokens since.\n"
    "Do not start new work here. Tell the user to open a fresh session and paste the start-here "
    "prompt. If work continued past the handover, re-run the `handover` skill before they switch "
    "so the doc is not stale."
)


def fire_point_drift(cfg: dict, cwd: str, limit: int = 8) -> str:
    """Feedback on where past handovers actually fired, versus AMBER.

    A guard that gets ignored until 250k is a guard that is not working. Telling
    the session its own median overshoot is what closes that loop."""
    try:
        docs, seen = [], set()
        dirs = [handover_dir(cwd)]
        sd = share_dir(cfg)
        if sd:
            dirs.append(sd)
        for d in dirs:
            if not d or not d.exists():
                continue
            for f in d.rglob("HANDOVER-*.md"):
                if f.name in seen:
                    continue
                seen.add(f.name)
                h = parse_handover(f)
                if h:
                    docs.append(h)
        if len(docs) < 3:
            return ""
        docs.sort(key=lambda h: h["mtime"], reverse=True)
        pts = sorted(h["context"] for h in docs[:limit])
        med = pts[len(pts) // 2]
        amber = cfg["thresholds"]["amber"]
        if med <= amber * 1.15:
            return ""
        return ("\nYour last %d handovers fired at a median of %s - %s past AMBER (%s). "
                "That overshoot is the expensive part. Hand over near AMBER, not at the block."
                % (len(pts), fmt_tok(med), fmt_tok(med - amber), fmt_tok(amber)))
    except Exception:
        return ""


def cmd_guard(payload: dict) -> int:
    cfg = load_config()
    if not cfg.get("enabled", True):
        return 0

    event = payload.get("hook_event_name") or "PostToolUse"
    session_id = payload.get("session_id") or ""
    tpath = payload.get("transcript_path")
    path = Path(tpath) if tpath else find_transcript(payload.get("cwd"), session_id)
    if not path or not path.exists():
        return 0

    st = load_state(session_id)
    size = path.stat().st_size

    # cheap escape hatch: don't re-read the transcript until it has grown enough
    if event == "PostToolUse":
        last_size = st.get("last_size", 0)
        if size - last_size < cfg.get("min_growth_bytes", 40000):
            return 0

    info = read_context(path)
    st["last_size"] = size
    if not info:
        save_state(session_id, st)
        return 0

    tokens = info["tokens"]
    window, known = window_detail(info["model"], tokens, cfg)
    band = band_for(tokens, window, cfg, known)

    prev_tokens = st.get("tokens", 0)
    fired = st.get("fired", [])
    last_notice = st.get("last_notice_tokens", 0)
    # context dropped a lot -> compaction or /clear: re-arm every band, and drop
    # the handover stamp - a fresh context is a new session in spirit
    if prev_tokens and tokens < prev_tokens * 0.7:
        fired = []
        last_notice = 0
        st.pop("handover_doc", None)
        st.pop("handover_at", None)
    st["tokens"] = tokens
    st["band"] = band
    st["window"] = window
    st["updated"] = time.time()

    if band == "green":
        st["fired"] = fired
        save_state(session_id, st)
        return 0

    # One nudge per band was too easy to scroll past: sessions took the AMBER
    # warning and still drifted 60-100k beyond it before handing over. Re-warn on
    # every further chunk of growth, so the cost stays in front of the model.
    renotify = max(5000, int(cfg.get("renotify_tokens", 25000)))
    new_band = band not in fired
    if not new_band and tokens - last_notice < renotify:
        st["fired"] = fired
        save_state(session_id, st)
        return 0

    if new_band:
        fired.append(band)
    st["fired"] = fired
    st["last_notice_tokens"] = tokens
    save_state(session_id, st)

    ratio = max(1, round(tokens / 15000))
    since = ""
    if last_notice and tokens > last_notice:
        since = (" %s more since the last warning, and all of it is re-sent every turn."
                 % fmt_tok(tokens - last_notice))
    done_doc = st.get("handover_doc")
    if done_doc:
        text = DONE_TEXT.format(doc=Path(done_doc).name, tok=f"{tokens:,}")
    else:
        text = GUARD_TEXT[band].format(
            tok=f"{tokens:,}", cost=ratio, since=since,
            drift=fire_point_drift(cfg, payload.get("cwd") or os.getcwd()),
        )
    pct = 100.0 * tokens / window
    ui = f"context {fmt_tok(tokens)}/{fmt_tok(window)} ({pct:.0f}%) - {band.upper()}"
    if done_doc:
        ui += " - handover written, start a fresh session"
    elif band == "amber":
        ui += " - wrap up the current step"
    elif band == "red":
        ui += " - /handover recommended"
    else:
        ui += " - HANDOVER NOW"

    out: dict = {
        "systemMessage": ui,
        "hookSpecificOutput": {"hookEventName": event, "additionalContext": text},
    }
    # Block every time we are over the line, not only the first time. The old
    # one-shot block was a speed bump: a session blocked once at 220k and then ran
    # on to 280k unimpeded. Writing the handover lifts the block.
    blocking = event == "PostToolUse" and not done_doc and (
        (band == "critical" and cfg.get("block_at_critical", True))
        or (band == "red" and cfg.get("block_at_red", False))
    )
    if blocking:
        out["decision"] = "block"
        out["reason"] = text
    emit(out)
    return 0


# ----------------------------------------------------------- session start ---
def cmd_sessionstart(payload: dict) -> int:
    cfg = load_config()
    cwd = payload.get("cwd") or os.getcwd()
    sid = payload.get("session_id") or ""
    max_age = float(cfg.get("offer_max_age_hours", 48))
    docs = pending_by_lane(cfg, cwd, max_age_hours=max_age)
    if not docs:
        return 0

    # A session that has already claimed a lane is only ever shown that lane.
    # Without this, a long-running session that resumes gets re-offered whatever
    # doc happens to be newest in the project - including a sibling session's.
    bound = session_lane(sid)
    if bound:
        docs = [d for d in docs if d["lane"] == bound]
        if not docs:
            return 0

    if len(docs) == 1:
        d = docs[0]
        age_h = (time.time() - d["mtime"]) / 3600.0
        # Being handed the doc is the pickup. Waiting for the agent to remember a
        # follow-up command left every offered handover stranded as `pending`,
        # which is how a project ends up with a queue of them.
        auto = bool(cfg.get("auto_consume", True))
        if auto:
            try:
                # cmd_consume reports on stdout, which in a hook is the JSON channel.
                with contextlib.redirect_stdout(io.StringIO()):
                    cmd_consume([d["path"], "--session", sid])
            except Exception:
                auto = False
        if not auto:
            bind_session_lane(sid, d["lane"])
        claim = ("It is already marked consumed, so nothing is needed to claim it."
                 if auto else
                 "Claim it with: python3 ~/.claude/handover/bin/ctx.py pickup "
                 f"--lane {d['lane']}")
        note = (
            "HANDOVER PENDING - one open thread of work in this project.\n"
            f"  lane  : {d['lane']}\n"
            f"  title : {d['title']}\n"
            f"  doc   : {d['path']}\n"
            f"  age   : {age_h:.0f}h ago, from machine '{d['machine']}'\n"
            + claim + "\n"
            "If the user's first message resumes this work - including a bare "
            "'continue' - read that doc FIRST and act on its Next steps. Do not "
            "ask them to paste a start-here prompt, and do not re-explore the "
            "codebase; the doc lists the files that matter. If their first "
            "message is clearly unrelated new work, ignore the doc - it stays on "
            "disk and `ctx.py list` still shows it."
        )
    else:
        lanes = "\n".join(
            f"  --lane {d['lane']:<24} {d['title']}  "
            f"({(time.time() - d['mtime']) / 3600.0:.0f}h ago, {d['machine']})"
            for d in docs)
        note = (
            f"{len(docs)} HANDOVERS PENDING in this project, on different threads "
            "of work:\n" + lanes + "\n"
            "None has been claimed, deliberately. Guessing which thread this "
            "session continues is how a handover gets applied to the wrong work, "
            "which is worse than having none: the session acts on someone else's "
            "plan with full confidence.\n"
            "- If the user's first message clearly matches one lane, claim it with "
            "`python3 ~/.claude/handover/bin/ctx.py pickup --lane <lane>` - that "
            "prints the doc and marks it consumed in one call.\n"
            "- If it is ambiguous - a bare 'continue' with several lanes open - ASK "
            "which one, listing the titles above. Never pick for them."
        )
    emit({
        "systemMessage": f"handover pending: {len(docs)} lane(s)",
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": note},
    })
    return 0


def handover_stamp(name: str) -> str:
    """The timestamp that identifies a handover across its copies.

    A doc mirrored to the shared folder is written as
    `HANDOVER-<stamp>-<machine>.md` beside the project's own
    `HANDOVER-<stamp>.md`. They are one handover, and treating them as two is
    how a project accumulates a queue that never drains: consuming either copy
    left the other pending, so the next session was offered it all over again.
    """
    m = re.match(r"HANDOVER-(\d{8}-\d{4})", name)
    return m.group(1) if m else name


def handover_copies(cfg: dict, cwd: str, stamp: str) -> list[Path]:
    """Every file on this machine that is the same handover as `stamp`."""
    found: list[Path] = []
    dirs = [handover_dir(cwd)]
    sd = share_dir(cfg)
    if sd:
        dirs.append(sd / project_slug(cwd))
    for d in dirs:
        if not d.is_dir():
            continue
        for q in d.glob(f"HANDOVER-{stamp}*.md"):
            found.append(q)
    return found


def pending_handovers(cfg: dict, cwd: str, max_age_hours: float = 48) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    dirs = [handover_dir(cwd)]
    sd = share_dir(cfg)
    if sd:
        dirs.append(sd / project_slug(cwd))
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("HANDOVER-*.md")):
            stamp = handover_stamp(p.name)
            if stamp in seen:
                continue
            try:
                head = p.read_text(errors="replace")[:1200]
            except Exception:
                continue
            if re.search(r"^status:\s*(consumed|superseded)", head, re.M):
                continue
            age = (time.time() - p.stat().st_mtime) / 3600.0
            if age > max_age_hours:
                continue
            m = re.search(r"^machine:\s*(\S+)", head, re.M)
            ml = re.search(r"^lane:\s*(\S+)", head, re.M)
            mt = re.search(r"^handover:\s*(.+)$", head, re.M)
            seen.add(stamp)
            out.append({"path": str(p), "mtime": p.stat().st_mtime,
                        "machine": m.group(1) if m else "?",
                        # docs written before lanes existed are all one lane
                        "lane": ml.group(1).strip() if ml else "main",
                        "title": mt.group(1).strip() if mt else "session handover"})
    out.sort(key=lambda x: -x["mtime"])
    return out


def pending_by_lane(cfg: dict, cwd: str, max_age_hours: float = 48) -> list[dict]:
    """The newest unclaimed handover per lane, newest lane first.

    Only one doc per lane can be live - a newer one supersedes it - so this is
    the true list of open threads of work in this project.
    """
    best: dict[str, dict] = {}
    for d in pending_handovers(cfg, cwd, max_age_hours):
        cur = best.get(d["lane"])
        if cur is None or d["mtime"] > cur["mtime"]:
            best[d["lane"]] = d
    return sorted(best.values(), key=lambda x: -x["mtime"])


def supersede_pending(cfg: dict, cwd: str, keep_stamp: str, lane: str = "") -> list[str]:
    """Retire the handovers this one replaces.

    A doc was only ever marked consumed by the session that picked it up, and
    that only happened if someone actually started a fresh one. Nothing else
    ever expired them, so a skipped `/clear` did not merely cost tokens — it
    stranded the doc, and every later session was offered work that had long
    since moved on. Writing a newer handover for the same project is proof the
    older one is finished with, so retire it here rather than waiting for a
    pickup that may never come.
    """
    retired: list[str] = []
    for d in pending_handovers(cfg, cwd, max_age_hours=float("inf")):
        stamp = handover_stamp(Path(d["path"]).name)
        if stamp == keep_stamp:
            continue
        # Only this lane's own predecessor is finished with. Retiring every
        # pending doc in the project would silently kill a concurrent session's
        # handover the moment this one wrote its own.
        if lane and d.get("lane", "main") != lane:
            continue
        # Retire the mirror alongside the project copy; one without the other
        # leaves the handover half-pending and it gets offered again.
        for path in handover_copies(cfg, cwd, stamp):
            try:
                txt = path.read_text()
                new = re.sub(
                    r"^status:\s*pending",
                    f"status: superseded by HANDOVER-{keep_stamp}.md at {now_iso()}",
                    txt, count=1, flags=re.M,
                )
                if new != txt:
                    path.write_text(new)
                    retired.append(str(path))
            except Exception:
                continue
    return retired


# ------------------------------------------------------------- status line ---
C_RESET = "\x1b[0m"
C_DIM = "\x1b[2m"
C_GREEN = "\x1b[32m"
C_YELLOW = "\x1b[33m"
C_RED = "\x1b[31m"
C_BOLD_RED = "\x1b[1;31m"
BAND_COLOR = {"green": C_GREEN, "amber": C_YELLOW, "red": C_RED, "critical": C_BOLD_RED}


def bar(frac: float, width: int = 8) -> str:
    filled = max(0, min(width, round(frac * width)))
    return "#" * filled + "." * (width - filled)


def cmd_statusline(payload: dict) -> int:
    cfg = load_config()
    cwd = (payload.get("workspace") or {}).get("current_dir") or payload.get("cwd") or ""
    model = ((payload.get("model") or {}).get("display_name")
             or (payload.get("model") or {}).get("id") or "")
    tpath = payload.get("transcript_path")
    path = Path(tpath) if tpath else find_transcript(cwd, payload.get("session_id"))

    parts = [f"{C_DIM}{Path(cwd).name or '~'}{C_RESET}"]
    if model:
        parts.append(f"{C_DIM}{model}{C_RESET}")

    info = read_context(path) if path and path.exists() else None
    if info:
        tokens = info["tokens"]
        window, known = window_detail(info["model"], tokens, cfg)
        band = band_for(tokens, window, cfg, known)
        col = BAND_COLOR[band]
        frac = tokens / window
        seg = f"{col}ctx {fmt_tok(tokens)} [{bar(frac)}] {100*frac:.0f}%{C_RESET}"
        if band == "amber":
            seg += f" {C_YELLOW}wrap up{C_RESET}"
        elif band == "red":
            seg += f" {C_RED}/handover{C_RESET}"
        elif band == "critical":
            seg += f" {C_BOLD_RED}HANDOVER NOW{C_RESET}"
        parts.append(seg)

    cost = payload.get("cost") or {}
    usd = cost.get("total_cost_usd")
    if isinstance(usd, (int, float)) and usd > 0:
        parts.append(f"{C_DIM}${usd:.2f}{C_RESET}")

    sys.stdout.write(f" {C_DIM}|{C_RESET} ".join(parts))
    return 0


# ----------------------------------------------------------------- status ---
def cmd_status(args: list[str]) -> int:
    cfg = load_config()
    as_json = "--json" in args
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    for i, a in enumerate(args):
        if a == "--cwd" and i + 1 < len(args):
            cwd = args[i + 1]
    path = None
    for i, a in enumerate(args):
        if a == "--transcript" and i + 1 < len(args):
            path = Path(args[i + 1])
        if a == "--session" and i + 1 < len(args):
            path = find_transcript(cwd, args[i + 1])
    if path is None:
        path = find_transcript(cwd, None)
    if not path or not path.exists():
        print("no transcript found for", cwd)
        return 1
    info = read_context(path)
    if not info:
        print("no usage records yet in", path)
        return 1
    tokens = info["tokens"]
    window, known = window_detail(info["model"], tokens, cfg)
    band = band_for(tokens, window, cfg, known)
    t = cfg["thresholds"]
    if as_json:
        print(json.dumps({"tokens": tokens, "window": window, "band": band,
                          "pct": round(100 * tokens / window, 1),
                          "model": info["model"], "transcript": str(path),
                          "thresholds": t}))
        return 0
    print(f"transcript : {path}")
    print(f"model      : {info['model'] or '?'}   window {fmt_tok(window)}"
          + ("" if known else "  (assumed - pin with assume_window in config.json)"))
    print(f"context    : {tokens:,} tokens  ({100*tokens/window:.1f}% of window)")
    print(f"band       : {band.upper()}   [amber {t['amber']:,} | red {t['red']:,} | critical {t['critical']:,}]")
    print(f"file size  : {path.stat().st_size/1048576:.1f} MB")
    return 0


# ------------------------------------------------------------------ facts ---
TOOL_FILE_KEYS = ("file_path", "notebook_path", "path")


def scan_transcript(path: Path, max_bytes: int = 40 * 1024 * 1024) -> dict:
    """Cheap single pass. Returns hard facts for a handover doc."""
    size = path.stat().st_size
    start = max(0, size - max_bytes)
    truncated = start > 0
    prompts: list[str] = []
    reads: dict[str, int] = {}
    writes: dict[str, int] = {}
    bash: list[str] = []
    todos: list[dict] = []
    tool_counts: dict[str, int] = {}
    subagents = 0
    turns = 0
    first_ts = last_ts = ""
    cwd = branch = ""

    with open(path, "rb") as f:
        if start:
            f.seek(start)
            f.readline()
        for raw in f:
            if len(raw) < 8:
                continue
            try:
                d = json.loads(raw)
            except Exception:
                continue
            t = d.get("type")
            ts = d.get("timestamp") or ""
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            cwd = d.get("cwd") or cwd
            branch = d.get("gitBranch") or branch
            if d.get("isSidechain"):
                continue
            msg = d.get("message") or {}
            content = msg.get("content")

            if t == "user" and not d.get("isMeta"):
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = "\n".join(b.get("text", "") for b in content
                                     if isinstance(b, dict) and b.get("type") == "text")
                text = text.strip()
                if (text and not text.startswith("<") and not text.startswith("Caveat:")
                        and "system-reminder" not in text[:60]):
                    prompts.append(text)
            elif t == "assistant":
                turns += 1
                if isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict) or b.get("type") != "tool_use":
                            continue
                        name = b.get("name") or "?"
                        tool_counts[name] = tool_counts.get(name, 0) + 1
                        inp = b.get("input") or {}
                        if name in ("Agent", "Task"):
                            subagents += 1
                        if name == "TodoWrite" and isinstance(inp.get("todos"), list):
                            todos = inp["todos"]
                        if name == "Bash" and isinstance(inp.get("command"), str):
                            bash.append(inp["command"].strip().replace("\n", " ")[:160])
                        fp = next((inp[k] for k in TOOL_FILE_KEYS
                                   if isinstance(inp.get(k), str)), None)
                        if fp:
                            bucket = writes if name in ("Edit", "Write", "NotebookEdit") else reads
                            bucket[fp] = bucket.get(fp, 0) + 1

    info = read_context(path) or {}
    return {
        "transcript": str(path), "truncated": truncated,
        "size_mb": round(size / 1048576, 1),
        "cwd": cwd, "branch": branch,
        "first_ts": first_ts, "last_ts": last_ts,
        "prompts": prompts, "reads": reads, "writes": writes,
        "bash": bash, "todos": todos, "tools": tool_counts,
        "subagents": subagents, "turns": turns,
        "context_tokens": info.get("tokens", 0), "model": info.get("model", ""),
    }


def _git(cwd: str, argv: list[str]) -> str:
    try:
        r = subprocess.run(["git", "-C", cwd] + argv, capture_output=True,
                           text=True, timeout=8)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def cmd_facts(args: list[str]) -> int:
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    path = None
    as_json = "--json" in args
    # A handover runs at peak context, where every line of tool output is re-sent
    # on every turn that follows. --tight keeps the facts that change what the
    # next session does and drops the ones that only describe this one.
    tight = "--tight" in args
    n_writes, n_reads, n_bash, n_git, n_prompts, n_chars = (
        (15, 0, 8, 12, 12, 200) if tight else (30, 20, 15, 25, 25, 300))
    for i, a in enumerate(args):
        if a == "--cwd" and i + 1 < len(args):
            cwd = args[i + 1]
        if a == "--transcript" and i + 1 < len(args):
            path = Path(args[i + 1])
    if path is None:
        path = find_transcript(cwd, None)
    if not path or not path.exists():
        print("no transcript found for", cwd, file=sys.stderr)
        return 1
    f = scan_transcript(path)
    if as_json:
        print(json.dumps(f, indent=2)[:200000])
        return 0

    def top(d: dict, n: int) -> list[tuple[str, int]]:
        return sorted(d.items(), key=lambda kv: -kv[1])[:n]

    print(f"# Session facts\n")
    print(f"- transcript: `{f['transcript']}` ({f['size_mb']} MB"
          + (", tail-scanned" if f["truncated"] else "") + ")")
    print(f"- cwd: `{f['cwd']}`" + (f"  branch: `{f['branch']}`" if f["branch"] else ""))
    print(f"- window: {f['first_ts'][:19]} -> {f['last_ts'][:19]}")
    print(f"- assistant turns: {f['turns']}   subagents: {f['subagents']}   "
          f"context now: {f['context_tokens']:,} tokens")
    if f["tools"]:
        print("- tool calls: " + ", ".join(f"{k} x{v}" for k, v in top(f["tools"], 10)))

    if f["writes"]:
        print("\n## Files modified")
        for p, n in top(f["writes"], n_writes):
            print(f"- `{p}` ({n} edit{'s' if n > 1 else ''})")
    if f["reads"] and n_reads:
        print("\n## Files read (top)")
        for p, n in top(f["reads"], n_reads):
            print(f"- `{p}` (x{n})")
    if f["todos"]:
        print("\n## Last todo list")
        for td in f["todos"]:
            mark = {"completed": "x", "in_progress": "~"}.get(td.get("status"), " ")
            print(f"- [{mark}] {td.get('content') or td.get('activeForm') or ''}")
    if f["bash"]:
        print("\n## Recent commands")
        seen: set[str] = set()
        out = []
        for c in reversed(f["bash"]):
            if c in seen:
                continue
            seen.add(c)
            out.append(c)
            if len(out) >= n_bash:
                break
        for c in reversed(out):
            print(f"- `{c}`")
    gcwd = f["cwd"] or cwd
    if (Path(gcwd) / ".git").exists() or _git(gcwd, ["rev-parse", "--is-inside-work-tree"]):
        st = _git(gcwd, ["status", "--short"])
        lg = _git(gcwd, ["log", "--oneline", "-5"])
        print("\n## Git state")
        print(f"- repo: `{gcwd}`  branch: `{_git(gcwd, ['rev-parse', '--abbrev-ref', 'HEAD']) or '?'}`")
        if st:
            print("- uncommitted:")
            for line in st.splitlines()[:n_git]:
                print(f"    {line}")
        else:
            print("- working tree clean")
        if lg:
            print("- recent commits:")
            for line in lg.splitlines():
                print(f"    {line}")

    if f["prompts"]:
        print("\n## User prompts, in order (verbatim, truncated)")
        for p in f["prompts"][-n_prompts:]:
            one = " ".join(p.split())
            print(f"- {one[:n_chars]}")
    return 0


# ---------------------------------------------------------------- savings ---
def rates_for(model: str, cfg: dict) -> dict:
    pr = cfg.get("pricing") or DEFAULT_CONFIG["pricing"]
    models = pr.get("models") or {}
    base = None
    for key, val in models.items():
        if model and (model == key or model.startswith(key)):
            base = val
            break
    base = base or pr.get("fallback") or {"input": 5.0, "output": 25.0}
    return {
        "input": base["input"],
        "output": base["output"],
        "cache_read": base["input"] * pr.get("cache_read_multiplier", 0.1),
        "cache_write": base["input"] * pr.get("cache_write_multiplier", 1.25),
    }


def session_usage(path: Path) -> dict:
    """Measured, not estimated: baseline start cost and everything re-sent so far."""
    baseline = 0
    resent = 0
    written = 0
    out = 0
    turns = 0
    peak = 0
    with open(path, "rb") as f:
        for raw in f:
            if b'"usage"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except Exception:
                continue
            if d.get("isSidechain"):
                continue
            u = (d.get("message") or {}).get("usage") or {}
            cr = u.get("cache_read_input_tokens") or 0
            cw = u.get("cache_creation_input_tokens") or 0
            tot = (u.get("input_tokens") or 0) + cr + cw
            if tot <= 0:
                continue
            turns += 1
            if baseline == 0:
                baseline = tot
            resent += cr
            written += cw
            out += u.get("output_tokens") or 0
            peak = max(peak, tot)
    return {"baseline": baseline, "resent": resent, "written": written,
            "output": out, "turns": turns, "peak": peak}


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def compute_savings(path: Path, cfg: dict, doc_text: str = "", turns: int | None = None) -> dict:
    u = session_usage(path)
    info = read_context(path) or {}
    ctx_now = info.get("tokens", u["peak"])
    model = info.get("model", "")
    r = rates_for(model, cfg)
    n = turns if turns is not None else int(cfg.get("projection_turns", 20))

    doc_tokens = estimate_tokens(doc_text) if doc_text else 0
    fresh_start = u["baseline"] + doc_tokens
    avoided_per_turn = max(0, ctx_now - fresh_start)
    avoided_total = avoided_per_turn * n

    usd = lambda tok, rate: tok * rate / 1_000_000
    return {
        "model": model,
        "context_now": ctx_now,
        "baseline": u["baseline"],
        "doc_tokens": doc_tokens,
        "fresh_start": fresh_start,
        "avoided_per_turn": avoided_per_turn,
        "projection_turns": n,
        "avoided_total": avoided_total,
        "avoided_usd": usd(avoided_total, r["cache_read"]),
        "spent_resent": u["resent"],
        "spent_resent_usd": usd(u["resent"], r["cache_read"]),
        "session_usd": (usd(u["resent"], r["cache_read"])
                        + usd(u["written"], r["cache_write"])
                        + usd(u["output"], r["output"])),
        "turns": u["turns"],
        "rate_cache_read": r["cache_read"],
    }


def render_savings(sv: dict) -> str:
    L = []
    L.append("## Savings from this handover")
    L.append("")
    L.append("| | tokens | at list price |")
    L.append("|---|---:|---:|")
    L.append(f"| Context carried by this session | {sv['context_now']:,} | |")
    L.append(f"| A fresh session seeded by this doc | {sv['fresh_start']:,} | |")
    L.append(f"| **Avoided on every future turn** | **{sv['avoided_per_turn']:,}** | "
             f"**${sv['avoided_per_turn']*sv['rate_cache_read']/1e6:,.3f}/turn** |")
    L.append(f"| Over the next {sv['projection_turns']} turns | "
             f"{sv['avoided_total']:,} | ${sv['avoided_usd']:,.2f} |")
    L.append("")
    L.append(f"This session has already re-sent **{sv['spent_resent']/1e6:,.1f}M tokens** "
             f"of context across {sv['turns']} turns "
             f"(~${sv['spent_resent_usd']:,.2f}); total session cost ~${sv['session_usd']:,.2f}.")
    L.append("")
    L.append(f"_Projection, not a bill. Measured baseline {sv['baseline']:,} tokens + "
             f"{sv['doc_tokens']:,} for this doc; avoided tokens valued at the cache-read rate "
             f"(${sv['rate_cache_read']:.2f}/M) for {sv['model'] or 'this model'}. "
             f"On a subscription the real currency is your usage allowance, not dollars._")
    return "\n".join(L)


# ------------------------------------------------------- realized savings ---
# `savings` projects what a handover about to be written would save. `savings
# --all` answers the other question: what did the handovers already written
# actually save, measured against the sessions that picked them up.

HANDOVER_RE = re.compile(rb"HANDOVER-\d{8}-\d{4}")


def parse_handover(path: Path) -> dict | None:
    """Frontmatter of one handover doc, or None if it is not one."""
    try:
        text = path.read_text(errors="replace")
        mtime = path.stat().st_mtime
    except OSError:
        return None
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return None
    fm = dict(re.findall(r"^([a-z_]+):\s*(.*)$", m.group(1), re.M))
    try:
        ctx = int(fm.get("context_at_handover", "0"))
    except ValueError:
        return None
    if ctx <= 0:
        return None
    stem = re.match(r"HANDOVER-\d{8}-\d{4}", path.stem)
    return {"path": path, "name": path.stem, "id": stem.group(0) if stem else path.stem,
            "project": fm.get("project") or path.parent.name,
            "session": fm.get("session", ""), "model": fm.get("model", ""),
            "written": fm.get("written", ""), "title": fm.get("handover", ""),
            "consumed_session": fm.get("consumed_session", ""),
            "context": ctx, "doc_tokens": estimate_tokens(text), "mtime": mtime}


def all_transcripts(cfg: dict) -> list[Path]:
    dirs = [PROJECTS] + [Path(os.path.expanduser(d))
                         for d in cfg.get("extra_transcript_dirs", [])]
    out: list[Path] = []
    for base in dirs:
        if base.is_dir():
            out.extend(base.glob("*/*.jsonl"))
    return out


def transcript_cwd(path: Path) -> str:
    """The project directory a transcript belongs to, from its first records."""
    try:
        with open(path, "rb") as f:
            for _ in range(8):
                raw = f.readline()
                if not raw:
                    break
                if b'"cwd"' not in raw:
                    continue
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                if d.get("cwd"):
                    return str(d["cwd"])
    except OSError:
        pass
    return ""


def discover_handovers(cfg: dict, cwd: str, transcripts: list[Path]) -> list[dict]:
    """Every handover doc reachable from here: this project, the share folder,
    the local fallback, and every project any transcript was recorded in."""
    dirs: list[Path] = [Path(cwd) / ".claude" / "handover"]
    sd = share_dir(cfg)
    if sd and sd.is_dir():
        dirs.extend(p for p in sd.iterdir() if p.is_dir())
    fallback = ROOT / "docs"
    if fallback.is_dir():
        dirs.extend(p for p in fallback.iterdir() if p.is_dir())
    seen_cwd: set[str] = set()
    for t in transcripts:
        c = transcript_cwd(t)
        if c and c not in seen_cwd:
            seen_cwd.add(c)
            dirs.append(Path(c) / ".claude" / "handover")
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("HANDOVER-*.md")):
            h = parse_handover(p)
            if not h:
                continue
            key = (h["project"], h["id"])
            if key in seen:
                continue
            seen.add(key)
            out.append(h)
    out.sort(key=lambda h: h["mtime"])
    return out


def scan_refs(path: Path) -> dict:
    """One pass over a transcript: its fresh-start baseline, how many turns it
    has taken, and which handover docs it mentions - each recorded with the turn
    it was first seen on, so a session that merely talks about a doc later is not
    mistaken for one that was seeded by it."""
    baseline = 0
    turns = 0
    peak = 0
    first = ""
    refs: dict[str, int] = {}
    try:
        with open(path, "rb") as f:
            for raw in f:
                if b"HANDOVER-" in raw:
                    for m in HANDOVER_RE.findall(raw):
                        refs.setdefault(m.decode(), turns)
                if b'"usage"' not in raw:
                    continue
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                if d.get("isSidechain"):
                    continue
                u = (d.get("message") or {}).get("usage") or {}
                tot = ((u.get("input_tokens") or 0)
                       + (u.get("cache_read_input_tokens") or 0)
                       + (u.get("cache_creation_input_tokens") or 0))
                if tot <= 0:
                    continue
                turns += 1
                peak = max(peak, tot)
                if not baseline:
                    baseline = tot
                    first = d.get("timestamp") or ""
    except OSError:
        pass
    return {"baseline": baseline, "turns": turns, "peak": peak, "first": first,
            "refs": refs, "session": path.stem}


def realized_savings(cfg: dict, cwd: str, days: int | None = None,
                     window: int = 3) -> dict:
    transcripts = all_transcripts(cfg)
    docs = discover_handovers(cfg, cwd, transcripts)
    if days:
        cutoff = time.time() - days * 86400
        docs = [h for h in docs if h["mtime"] >= cutoff]
    if not docs:
        return {"rows": [], "unused": [], "total": 0, "usd": 0.0, "docs": 0}

    # only transcripts that could possibly have picked one of these up
    oldest = min(h["mtime"] for h in docs) - 86400
    by_name: dict[str, list[dict]] = {}
    for t in transcripts:
        try:
            if t.stat().st_mtime < oldest:
                continue
        except OSError:
            continue
        s = scan_refs(t)
        if not s["turns"] or not s["refs"]:
            continue
        for name in s["refs"]:
            by_name.setdefault(name, []).append(s)

    # A doc is "picked up" by the earliest-starting session that read it within
    # its first few turns and did not write it. A session is credited once, to
    # the latest doc it read - otherwise a chain of superseded handovers counts
    # the same session's turns several times.
    claims: dict[str, dict] = {}
    for h in docs:
        cands = [s for s in by_name.get(h["id"], [])
                 if h["session"] and not s["session"].startswith(h["session"])
                 and s["refs"].get(h["id"], window + 1) <= window]
        if h["consumed_session"]:
            exact = [s for s in by_name.get(h["id"], [])
                     if s["session"].startswith(h["consumed_session"])]
            cands = exact or cands
        if not cands:
            continue
        s = min(cands, key=lambda s: s["first"])
        prev = claims.get(s["session"])
        if prev is None or h["mtime"] > prev["handover"]["mtime"]:
            claims[s["session"]] = {"handover": h, "successor": s}

    rows = []
    total = 0
    usd = 0.0
    for c in claims.values():
        h, s = c["handover"], c["successor"]
        per_turn = max(0, h["context"] - s["baseline"])
        saved = per_turn * s["turns"]
        rate = rates_for(h["model"], cfg)["cache_read"]
        total += saved
        usd += saved * rate / 1_000_000
        rows.append({"handover": h, "successor": s, "per_turn": per_turn,
                     "saved": saved, "usd": saved * rate / 1_000_000})
    rows.sort(key=lambda r: r["handover"]["mtime"])
    taken = {r["handover"]["id"] for r in rows}
    unused = [h for h in docs if h["id"] not in taken]
    return {"rows": rows, "unused": unused, "total": total, "usd": usd, "docs": len(docs)}


def render_realized(res: dict) -> str:
    L: list[str] = []
    rows = res["rows"]
    if not res["docs"]:
        return "No handover docs found. Write one with /handover first."
    if not rows:
        L.append("No handover has been picked up by a fresh session yet.")
        L.append(f"{len(res['unused'])} written and waiting.")
        return "\n".join(L)
    L.append(f"Realized handover savings - {len(rows)} of {res['docs']} handovers picked up\n")
    L.append(f"  {'project':<16} {'written':<11} {'ctx@handover':>12} {'fresh start':>11} "
             f"{'turns':>6} {'saved':>9}")
    for r in rows:
        h, s = r["handover"], r["successor"]
        when = h["written"][5:16] if len(h["written"]) > 15 else h["written"]
        L.append(f"  {h['project'][:16]:<16} {when:<11} {h['context']:>12,} "
                 f"{s['baseline']:>11,} {s['turns']:>6} {r['saved']/1e6:>8.1f}M")
    L.append("")
    L.append(f"  tokens not re-sent : {res['total']/1e6:,.1f}M")
    L.append(f"  at list price      : ${res['usd']:,.2f} (cache-read rate)")
    if res["unused"]:
        L.append("")
        L.append(f"  not traced to a fresh session ({len(res['unused'])}):")
        for h in res["unused"][-6:]:
            when = h["written"][5:16] if len(h["written"]) > 15 else h["written"]
            L.append(f"    {h['project'][:16]:<16} {when:<11} ctx {h['context']:,}")
        L.append("    (superseded by a later handover, still waiting, or picked up")
        L.append("     without the doc being named - run `consume` to make it exact)")
    L.append("")
    L.append("  A floor, not a bill. Each row assumes the old session's context would have")
    L.append("  stayed flat at its handover size; in practice it kept growing, so the real")
    L.append("  number is higher. A session counts as a pickup only if it named the doc in")
    L.append("  its first few turns, and is credited once, to the newest doc it read.")
    L.append("  On a subscription the currency is your usage allowance, not dollars.")
    return "\n".join(L)


def savings_all(args: list[str], cfg: dict, cwd: str) -> int:
    days = None
    window = 3
    for i, a in enumerate(args):
        if a == "--days" and i + 1 < len(args):
            days = int(args[i + 1])
        if a == "--window" and i + 1 < len(args):
            window = int(args[i + 1])
    res = realized_savings(cfg, cwd, days, window)
    if "--json" in args:
        print(json.dumps({
            "total_tokens": res["total"],
            "total_usd": round(res["usd"], 2),
            "handovers": res["docs"],
            "picked_up": len(res["rows"]),
            "rows": [{"project": r["handover"]["project"],
                      "written": r["handover"]["written"],
                      "doc": r["handover"]["id"],
                      "context_at_handover": r["handover"]["context"],
                      "fresh_start": r["successor"]["baseline"],
                      "successor": r["successor"]["session"][:8],
                      "turns": r["successor"]["turns"],
                      "saved_per_turn": r["per_turn"],
                      "saved_tokens": r["saved"],
                      "saved_usd": round(r["usd"], 2)} for r in res["rows"]],
            "waiting": [{"project": h["project"], "written": h["written"],
                         "doc": h["id"], "context_at_handover": h["context"]}
                        for h in res["unused"]],
        }, indent=2))
    else:
        print(render_realized(res))
    return 0


def cmd_savings(args: list[str]) -> int:
    cfg = load_config()
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    for i, a in enumerate(args):
        if a == "--cwd" and i + 1 < len(args):
            cwd = args[i + 1]
    if "--all" in args:
        return savings_all(args, cfg, cwd)
    doc = ""
    turns = None
    path = None
    for i, a in enumerate(args):
        if a == "--cwd" and i + 1 < len(args):
            cwd = args[i + 1]
        if a == "--doc" and i + 1 < len(args) and Path(args[i + 1]).exists():
            doc = Path(args[i + 1]).read_text()
        if a == "--turns" and i + 1 < len(args):
            turns = int(args[i + 1])
        if a == "--transcript" and i + 1 < len(args):
            path = Path(args[i + 1])
    path = path or find_transcript(cwd, None)
    if not path or not path.exists():
        print("no transcript found for", cwd, file=sys.stderr)
        return 1
    print(render_savings(compute_savings(path, cfg, doc, turns)))
    return 0


# ------------------------------------------------------------------ write ---
FRONTMATTER = """---
handover: {title}
project: {project}
lane: {lane}
cwd: {cwd}
branch: {branch}
machine: {machine}
session: {session}
model: {model}
context_at_handover: {tokens}
written: {when}
status: pending
---

"""


def cmd_write(args: list[str]) -> int:
    cfg = load_config()
    body_file = None
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    title = "session handover"
    lane = ""
    quiet = "--quiet" in args
    for i, a in enumerate(args):
        if a == "--body" and i + 1 < len(args):
            body_file = Path(args[i + 1])
        if a == "--cwd" and i + 1 < len(args):
            cwd = args[i + 1]
        if a == "--title" and i + 1 < len(args):
            title = args[i + 1]
        if a == "--lane" and i + 1 < len(args):
            lane = lane_slug(args[i + 1])
    if not body_file or not body_file.exists():
        print("usage: ctx.py write --body <file.md> [--cwd DIR] [--title T]", file=sys.stderr)
        return 1

    body = body_file.read_text()
    tpath = find_transcript(cwd, None)
    info = (read_context(tpath) if tpath and tpath.exists() else None) or {}
    if not transcript_owns_cwd(tpath, info, cwd):
        # A foreign session resolved by env id: none of its numbers, its lane or
        # its state file belong to this doc. Write the doc without them.
        tpath, info = None, {}
    savings = None
    if tpath and tpath.exists():
        try:
            savings = compute_savings(tpath, cfg, body)
            body = body.rstrip() + "\n\n" + render_savings(savings) + "\n"
        except Exception:
            savings = None
    # A session that already picked up a lane stays in it, so a chain of
    # handovers on one thread of work keeps a single identity across sessions.
    _sid = info.get("session_id") or ""
    if not lane:
        lane = session_lane(_sid) or lane_slug(title)

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    doc = FRONTMATTER.format(
        title=title, project=project_slug(cwd), lane=lane, cwd=cwd,
        branch=info.get("git_branch") or "-", machine=MACHINE,
        session=(info.get("session_id") or "-")[:8], model=info.get("model") or "-",
        tokens=info.get("tokens", 0), when=now_iso(),
    ) + body.strip() + "\n"

    d = handover_dir(cwd)
    out = d / f"HANDOVER-{stamp}.md"
    out.write_text(doc)
    (d / "LATEST.md").write_text(doc)
    written = [str(out)]

    sd = share_dir(cfg)
    if sd:
        sub = sd / project_slug(cwd)
        sub.mkdir(parents=True, exist_ok=True)
        mirror = sub / f"HANDOVER-{stamp}-{MACHINE}.md"
        mirror.write_text(doc)
        written.append(str(mirror))

    # Everything this doc replaces is finished with by definition.
    retired = supersede_pending(cfg, cwd, stamp, lane)

    # pull the start-here prompt out of the first fenced block after the heading
    prompt = ""
    m = re.search(r"#+\s*Start-?here prompt.*?```(?:\w+)?\n(.*?)```", doc, re.S | re.I)
    if m:
        prompt = m.group(1).strip()
    if prompt:
        (d / "PROMPT.txt").write_text(prompt + "\n")
        written.append(str(d / "PROMPT.txt"))
        if cfg.get("clipboard", True) and sys.platform == "darwin":
            try:
                subprocess.run(["pbcopy"], input=prompt.encode(), timeout=5)
                written.append("(copied to clipboard)")
            except Exception:
                pass
    # Let the guard in this session know the doc exists: it keeps nagging, but
    # stops blocking, so following the guard's own instruction cannot wedge a turn.
    sid = info.get("session_id") or ""
    if sid:
        try:
            stw = load_state(sid)
            stw["handover_doc"] = str(out)
            stw["handover_at"] = time.time()
            stw["handover_tokens"] = info.get("tokens", 0)
            stw["lane"] = lane
            save_state(sid, stw)
        except Exception:
            pass

    for w in written:
        print(w)
    for r in retired:
        print("superseded:", r)
    print("lane:", lane)
    if savings and not quiet:
        print()
        print(render_savings(savings))
    return 0


def cmd_list(args: list[str]) -> int:
    cfg = load_config()
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    for i, a in enumerate(args):
        if a == "--cwd" and i + 1 < len(args):
            cwd = args[i + 1]
    docs = pending_handovers(cfg, cwd, max_age_hours=24 * 365)
    if not docs:
        print("no handovers for", project_slug(cwd))
        return 0
    for d in docs[:20]:
        age = (time.time() - d["mtime"]) / 3600.0
        print(f"{age:6.1f}h ago  [{d['lane']:<20}] [{d['machine']:<12}] {d['path']}")
    return 0


def cmd_show(args: list[str]) -> int:
    cfg = load_config()
    if args and Path(args[0]).exists():
        print(Path(args[0]).read_text())
        return 0
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    docs = pending_handovers(cfg, cwd, max_age_hours=24 * 365)
    if not docs:
        print("no handovers for", project_slug(cwd))
        return 1
    print(Path(docs[0]["path"]).read_text())
    return 0


def cmd_consume(args: list[str]) -> int:
    if not args:
        print("usage: ctx.py consume <handover.md>", file=sys.stderr)
        return 1
    p = Path(args[0])
    if not p.exists():
        print("not found:", p, file=sys.stderr)
        return 1
    txt = p.read_text()
    sid = ""
    for i, a in enumerate(args):
        if a == "--session" and i + 1 < len(args):
            sid = args[i + 1]
    if not sid:
        m = re.search(r"^cwd:\s*(.+)$", txt[:2000], re.M)
        t = find_transcript((m.group(1).strip() if m else None) or os.getcwd(), None)
        sid = t.stem if t else ""
    def mark(doc: Path) -> bool:
        try:
            body = doc.read_text()
        except Exception:
            return False
        out = re.sub(r"^status:\s*pending", f"status: consumed by {MACHINE} at {now_iso()}",
                     body, count=1, flags=re.M)
        if sid and not re.search(r"^consumed_session:", out, re.M):
            out = re.sub(r"^(status:.*)$", rf"\1\nconsumed_session: {sid}", out, count=1, flags=re.M)
        if out == body:
            return False
        doc.write_text(out)
        return True

    # The same handover exists as a project copy and, when a shared folder is
    # configured, a mirror under a different name. Marking one and leaving the
    # other pending is what kept re-offering work that had already been picked
    # up, so retire every copy together.
    cfg = load_config()
    m_cwd = re.search(r"^cwd:\s*(.+)$", txt[:2000], re.M)
    targets = {p.resolve()}
    if m_cwd:
        for q in handover_copies(cfg, m_cwd.group(1).strip(), handover_stamp(p.name)):
            targets.add(q.resolve())

    for doc in sorted(targets):
        if mark(doc):
            print("marked consumed:", doc)
    # Pin this session to the lane it just claimed: it will not be offered a
    # different one, and any handover it writes later stays on this thread.
    ml = re.search(r"^lane:\s*(\S+)", txt[:2000], re.M)
    bind_session_lane(sid, ml.group(1).strip() if ml else "main")
    return 0


def cmd_pickup(args: list[str]) -> int:
    """Resolve, print and claim the handover a fresh session should continue.

    Prints the doc body, not just its path. Locating the doc and then reading it
    were two round trips at the very start of a session, and the doc is only
    useful once it is in context anyway.

    Exit 2 means ambiguous: several lanes are open and the caller must ask which
    one rather than choose.
    """
    cfg = load_config()
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    lane = sid = ""
    for i, a in enumerate(args):
        if a == "--cwd" and i + 1 < len(args):
            cwd = args[i + 1]
        if a == "--lane" and i + 1 < len(args):
            lane = lane_slug(args[i + 1])
        if a == "--session" and i + 1 < len(args):
            sid = args[i + 1]
    docs = pending_by_lane(cfg, cwd,
                           max_age_hours=float(cfg.get("offer_max_age_hours", 48)))
    if not lane and sid:
        lane = session_lane(sid)
    if lane:
        docs = [d for d in docs if d["lane"] == lane]
    if not docs:
        print(f"no pending handover for {project_slug(cwd)}"
              + (f" lane '{lane}'" if lane else ""), file=sys.stderr)
        return 1
    if len(docs) > 1:
        print("AMBIGUOUS - several lanes are open. Ask which one:", file=sys.stderr)
        for d in docs:
            age = (time.time() - d["mtime"]) / 3600.0
            print(f"  --lane {d['lane']:<24} {d['title']}  ({age:.0f}h ago)",
                  file=sys.stderr)
        return 2
    d = docs[0]
    if not sid:
        t = find_transcript(cwd, None)
        sid = t.stem if t else ""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_consume([d["path"], "--session", sid])
    except Exception:
        pass
    print(f"# picked up lane '{d['lane']}' - {d['path']}\n")
    print(Path(d["path"]).read_text())
    return 0


def cmd_brief(args: list[str]) -> int:
    """status + facts in one call, tight by default.

    Two commands meant two round trips, each re-sending the whole 175k-token
    context that the handover exists to escape. Pass --full for the long form.
    """
    a = list(args)
    if "--full" not in a and "--tight" not in a:
        a.append("--tight")
    rc = cmd_status([x for x in a if x not in ("--tight", "--full")])
    print()
    return cmd_facts(a) or rc


# ----------------------------------------------------------------- report ---
def cmd_report(args: list[str]) -> int:
    days = 7
    for i, a in enumerate(args):
        if a == "--days" and i + 1 < len(args):
            days = int(args[i + 1])
    cutoff = time.time() - days * 86400
    dirs = [PROJECTS] + [Path(os.path.expanduser(d))
                         for d in load_config().get("extra_transcript_dirs", [])]
    rows = []
    for base in dirs:
        if not base.is_dir():
            continue
        for p in base.glob("*/*.jsonl"):
            try:
                stt = p.stat()
            except OSError:
                continue
            if stt.st_mtime < cutoff:
                continue
            peak = 0
            cache_read = 0
            turns = 0
            heavy = 0
            with open(p, "rb") as f:
                for raw in f:
                    if b'"cache_read_input_tokens"' not in raw:
                        continue
                    try:
                        d = json.loads(raw)
                    except Exception:
                        continue
                    u = (d.get("message") or {}).get("usage") or {}
                    cr = u.get("cache_read_input_tokens") or 0
                    tot = ((u.get("input_tokens") or 0) + cr
                           + (u.get("cache_creation_input_tokens") or 0))
                    if tot <= 0:
                        continue
                    turns += 1
                    cache_read += cr
                    peak = max(peak, tot)
                    if tot > 150000:
                        heavy += 1
            if turns:
                rows.append({"path": p, "project": p.parent.name, "peak": peak,
                             "resent": cache_read, "turns": turns, "heavy": heavy,
                             "mtime": stt.st_mtime})
    if not rows:
        print(f"no transcripts in the last {days} days")
        return 0
    rows.sort(key=lambda r: -r["resent"])
    total_resent = sum(r["resent"] for r in rows)
    total_turns = sum(r["turns"] for r in rows)
    total_heavy = sum(r["heavy"] for r in rows)
    print(f"Context report - last {days} days, {len(rows)} sessions, {total_turns} model turns\n")
    print(f"  tokens re-sent as context : {total_resent/1e6:,.1f}M")
    print(f"  turns above 150k context  : {total_heavy} ({100*total_heavy/max(1,total_turns):.0f}%)")
    print(f"  avg context per turn      : {fmt_tok(int(total_resent/max(1,total_turns)))}\n")
    print(f"  {'project':<34} {'peak ctx':>9} {'turns':>6} {'>150k':>6} {'re-sent':>9}")
    for r in rows[:15]:
        # Project dirs are the cwd with every non-alphanumeric turned into "-",
        # so every one of them opens with this machine's home prefix. Strip that
        # exact prefix - deriving it beats hardcoding one machine's username.
        home_slug = re.sub(r"[^A-Za-z0-9]", "-", str(HOME))
        proj = r["project"]
        if proj.startswith(home_slug + "-"):
            proj = proj[len(home_slug) + 1:]
        proj = proj[-34:]
        print(f"  {proj:<34} {fmt_tok(r['peak']):>9} {r['turns']:>6} {r['heavy']:>6} "
              f"{r['resent']/1e6:>8.1f}M")
    print("\n  Sessions with many >150k turns are the ones to hand over earlier.")
    return 0


# ---------------------------------------------------------------- install ---
CMD = '"$HOME/.claude/handover/bin/ctx.py"'
HOOK_SPECS = {
    "PostToolUse": ("guard", "context guard"),
    "UserPromptSubmit": ("guard", "context guard"),
    "SessionStart": ("sessionstart", "handover check"),
}


def cmd_install(args: list[str]) -> int:
    project = None
    force_status = "--force-statusline" in args
    for i, a in enumerate(args):
        if a == "--project" and i + 1 < len(args):
            project = Path(args[i + 1])
    target = (project / ".claude" / "settings.json") if project else (HOME / ".claude" / "settings.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    settings = {}
    if target.exists():
        raw = target.read_text()
        try:
            settings = json.loads(raw)
        except Exception:
            print(f"ERROR: {target} is not valid JSON - fix it first", file=sys.stderr)
            return 1
        backup = target.with_suffix(f".json.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        backup.write_text(raw)
        print(f"backup: {backup}")

    hooks = settings.setdefault("hooks", {})
    for event, (sub, label) in HOOK_SPECS.items():
        entries = hooks.setdefault(event, [])
        entries[:] = [e for e in entries
                      if not any("ctx.py" in (h.get("command") or "")
                                 for h in (e.get("hooks") or []))]
        entries.append({"hooks": [{
            "type": "command",
            "command": f"python3 {CMD} {sub}",
            "timeout": 10,
            "statusMessage": label,
        }]})

    if force_status or "statusLine" not in settings:
        settings["statusLine"] = {"type": "command",
                                  "command": f"python3 {CMD} statusline",
                                  "padding": 0}
    else:
        print("note: statusLine already set, left alone (use --force-statusline to replace)")

    target.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"installed into {target}")
    print("hooks: " + ", ".join(HOOK_SPECS))
    print("open /hooks once (or restart Claude Code) to load them")
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        print(f"wrote default config: {CONFIG_PATH}")
    return 0


def cmd_doctor(args: list[str]) -> int:
    ok = True
    print(f"ctx.py {VERSION} on {MACHINE}  (python {sys.version.split()[0]})")
    print(f"root       : {ROOT}  {'OK' if ROOT.is_dir() else 'MISSING'}")
    cfg = load_config()
    print(f"config     : {CONFIG_PATH}  {'OK' if CONFIG_PATH.exists() else 'defaults'}")
    print(f"thresholds : amber {cfg['thresholds']['amber']:,} | red {cfg['thresholds']['red']:,}"
          f" | critical {cfg['thresholds']['critical']:,}")
    sd = share_dir(cfg)
    print(f"share dir  : {sd if sd else '(not set - single machine only)'}")
    st = HOME / ".claude" / "settings.json"
    try:
        s = json.loads(st.read_text())
    except Exception:
        print(f"settings   : {st} UNREADABLE")
        return 1
    for event in HOOK_SPECS:
        found = any("ctx.py" in (h.get("command") or "")
                    for e in (s.get("hooks", {}).get(event) or [])
                    for h in (e.get("hooks") or []))
        print(f"hook {event:<16}: {'wired' if found else 'MISSING'}")
        ok = ok and found
    sl = (s.get("statusLine") or {}).get("command", "")
    print(f"statusLine : {'wired' if 'ctx.py' in sl else 'not using ctx.py'}")
    # The pickup subagents keep verification output and git archaeology out of a
    # fresh main context. Missing ones are not fatal - the session just pays for
    # that reading itself - so this reports without failing the doctor.
    for agent in ("verify-pickup", "handover-staleness"):
        ap = HOME / ".claude" / "agents" / f"{agent}.md"
        print(f"agent {agent:<18}: {'ok' if ap.exists() else 'missing (optional)'}")
    cwd = os.getcwd()
    p = find_transcript(cwd, None)
    print(f"transcript : {p if p else 'none found for ' + cwd}")
    if p:
        info = read_context(p)
        if info:
            w, kn = window_detail(info["model"], info["tokens"], cfg)
            print(f"context    : {info['tokens']:,} tokens -> {band_for(info['tokens'], w, cfg, kn).upper()}")
    return 0 if ok else 1


# ------------------------------------------------------------------- main ---
def read_stdin_json() -> dict:
    try:
        data = sys.stdin.read()
        return json.loads(data) if data.strip() else {}
    except Exception:
        return {}


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 0
    cmd, args = argv[1], argv[2:]
    try:
        if cmd == "guard":
            return cmd_guard(read_stdin_json())
        if cmd == "sessionstart":
            return cmd_sessionstart(read_stdin_json())
        if cmd == "statusline":
            return cmd_statusline(read_stdin_json())
        if cmd == "status":
            return cmd_status(args)
        if cmd == "facts":
            return cmd_facts(args)
        if cmd == "brief":
            return cmd_brief(args)
        if cmd == "pickup":
            return cmd_pickup(args)
        if cmd == "write":
            return cmd_write(args)
        if cmd == "list":
            return cmd_list(args)
        if cmd == "show":
            return cmd_show(args)
        if cmd == "consume":
            return cmd_consume(args)
        if cmd == "savings":
            return cmd_savings(args)
        if cmd == "report":
            return cmd_report(args)
        if cmd == "install":
            return cmd_install(args)
        if cmd == "doctor":
            return cmd_doctor(args)
    except BrokenPipeError:
        return 0
    except Exception as e:
        # a hook must never break the session
        if cmd in ("guard", "sessionstart", "statusline"):
            return 0
        print(f"ctx.py {cmd}: {e}", file=sys.stderr)
        return 1
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
