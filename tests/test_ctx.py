#!/usr/bin/env python3
"""Tests for the parts of ctx.py that can lose your work.

    python3 tests/test_ctx.py

Stdlib only, no test runner, same as the tool. Everything runs against temp
directories with ROOT/CLAUDE_DIR/STATE_DIR monkeypatched onto them and a
`file://` update source, so a run never touches the real install, the real
~/.claude/settings.json, or the network.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CTX = REPO / "bin" / "ctx.py"

FAILURES = []


def check(label, got, want):
    good = got == want
    if not good:
        FAILURES.append(label)
    print(f"{'PASS' if good else 'FAIL'}  {label}"
          + ("" if good else f": got {got!r}, want {want!r}"))


def load_ctx():
    """A fresh module object, so one test's monkeypatching cannot leak."""
    spec = importlib.util.spec_from_file_location("ctx_under_test", CTX)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# --------------------------------------------------------------- the writer ---
def test_writer_concurrency():
    """Five sessions finishing at once must produce five handovers.

    The stamp was minute-resolution and the write a bare write_text(), so they
    used to produce one -- the four that lost the race were erased. That is the
    exact case lanes exist to support.
    """
    print("\n-- writer --")
    tmp = Path(tempfile.mkdtemp())
    body = tmp / "body.md"
    body.write_text("## Goal\ntest\n\n## Start-here prompt\n\n```text\ncontinue\n```\n")
    # Point the subprocesses at an empty install: no config.json means
    # DEFAULT_CONFIG, which has no share_dir, so nothing is mirrored to iCloud.
    env = dict(os.environ, CLAUDE_HANDOVER_ROOT=str(tmp / "install"))
    procs = [subprocess.Popen(
        [sys.executable, str(CTX), "write", "--body", str(body), "--cwd", str(tmp),
         "--title", f"t{i}", "--lane", f"lane{i}", "--quiet"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env) for i in range(5)]
    for p in procs:
        p.wait()

    d = tmp / ".claude" / "handover"
    docs = sorted(p.name for p in d.glob("HANDOVER-*.md"))
    check("five concurrent writes -> five docs", len(docs), 5)
    check("filenames are unique", len(set(docs)), len(docs))

    m = load_ctx()
    stamps = {m.handover_stamp(n) for n in docs}
    # The stamp is the identity that pairs a doc with its mirror, so a unique
    # filename is not enough -- the stamp itself has to be unique too.
    check("stamps are unique", len(stamps), 5)
    check("stamps are seconds-resolution", all(len(s) == 15 for s in stamps), True)

    # LATEST is per-lane; as one file per directory the lanes overwrote each other.
    check("a LATEST per lane", len(list(d.glob("LATEST-lane?.md"))), 5)
    check("a PROMPT per lane", len(list(d.glob("PROMPT-lane?.txt"))), 5)
    check("bare LATEST.md kept as an alias", (d / "LATEST.md").exists(), True)
    shutil.rmtree(tmp)


def test_handover_stamp_back_compat():
    print("\n-- stamp parsing --")
    m = load_ctx()
    check("seconds form", m.handover_stamp("HANDOVER-20260910-172458.md"), "20260910-172458")
    check("seconds mirror", m.handover_stamp("HANDOVER-20260910-172458-Yohanas-MBP.md"),
          "20260910-172458")
    # Every doc written before 1.6.0 is the four-digit form and must still pair
    # with its mirror, or a project's queue silently stops draining.
    check("legacy form", m.handover_stamp("HANDOVER-20260910-1724.md"), "20260910-1724")
    check("legacy mirror", m.handover_stamp("HANDOVER-20260910-1724-Yohanas-MBP.md"),
          "20260910-1724")

    # savings/report keys docs by a second id space, matched against the names it
    # scrapes out of transcripts. Truncating a seconds filename to its minute in
    # one of the two and not the other silently stops them matching.
    from pathlib import Path as P
    for name in ("HANDOVER-20260910-172458", "HANDOVER-20260910-1724"):
        scraped = m.HANDOVER_RE.findall(f"see {name}.md".encode())
        parsed = __import__("re").match(
            r"HANDOVER-(?:\d{8}-\d{6}|\d{8}-\d{4})", P(name + ".md").stem)
        check(f"scraped id == parsed id for {name}",
              (scraped[0].decode() if scraped else None),
              (parsed.group(0) if parsed else None))


def test_latest_symlink_is_not_followed():
    """write_text() opens 'w', follows a symlink and truncates its target."""
    print("\n-- LATEST.md symlink --")
    m = load_ctx()
    tmp = Path(tempfile.mkdtemp())
    victim = tmp / "HANDOVER-20260101-000000.md"
    victim.write_text("the handover this used to destroy\n")
    link = tmp / "LATEST.md"
    link.symlink_to(victim)
    m.write_regular(link, "new content\n")
    check("target survives", victim.read_text(), "the handover this used to destroy\n")
    check("link replaced by a regular file", link.is_symlink(), False)
    shutil.rmtree(tmp)


# -------------------------------------------------------- the update channel ---
def sandbox_update():
    """A module pointed at a temp install and a file:// published tree."""
    m = load_ctx()
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "published"
    rels = ("bin/ctx.py", "skills/handover/SKILL.md",
            "agents/verify-pickup.md", "agents/handover-staleness.md")
    for rel in rels:
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, src / rel)
    shutil.copy(REPO / "update-manifest.json", src / "update-manifest.json")

    m.ROOT = tmp / "install"
    m.CLAUDE_DIR = tmp / "claude"
    m.STATE_DIR = m.ROOT / "state"
    m.STATE_DIR.mkdir(parents=True)
    for rel in rels:
        dest = (m.ROOT if rel.startswith("bin/") else m.CLAUDE_DIR) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, dest)

    cfg = dict(m.DEFAULT_CONFIG)
    cfg["update"] = dict(m.DEFAULT_CONFIG["update"], source=f"file://{src}/")
    return m, cfg, tmp, src


def test_update_refuses_local_edits():
    """The guard that would have caught a fix living on one machine for 3 days."""
    print("\n-- update: local edits --")
    m, cfg, tmp, _ = sandbox_update()
    MARK = "# ZZ-uncommitted-local-edit-ZZ"

    check("a clean install is not drifted", m.drifted_files(cfg, 10), [])
    check("identical tree -> current", m.apply_update(cfg)["status"], "current")

    p = m.ROOT / "bin/ctx.py"
    p.write_text(p.read_text() + f"\n{MARK}\n")
    check("hand-patch detected", m.drifted_files(cfg, 10), ["root:bin/ctx.py"])
    res = m.apply_update(cfg)
    check("apply refuses", res["status"], "drifted")
    check("and names the file", res["files"], ["root:bin/ctx.py"])
    check("the edit survives the refusal", MARK in p.read_text(), True)

    res = m.apply_update(cfg, force=True)
    check("--force applies anyway", res["status"], "updated")
    check("and discards the edit", MARK in p.read_text(), False)
    check("a backup was taken", (m.ROOT / "backup" / m.VERSION / "manifest.json").exists(), True)
    shutil.rmtree(tmp)


def test_rollback_pins_from_applied_version():
    """Rollback used to read the version off `notice`, which a later failed
    check overwrites with a shape that has no version in it -- so it pinned to
    "" and the next check reinstalled what you just backed out of."""
    print("\n-- update: rollback --")
    m, cfg, tmp, _ = sandbox_update()
    m.apply_update(cfg, force=True)
    check("applied_version recorded", m.load_update_state().get("applied_version"), m.VERSION)
    m.record_notice("failed", error="a later 503, with no `to` field")
    check("rollback ok", m.update_rollback(quiet=True), 0)
    check("pinned from applied_version", m.load_update_state().get("skip_version"), m.VERSION)
    shutil.rmtree(tmp)


def test_failure_backoff_and_notice_lifecycle():
    print("\n-- update: notices --")
    m, cfg, tmp, _ = sandbox_update()

    m.record_failure("HTTPError: 503 Backend.max_conn reached", cfg)
    st = m.load_update_state()
    wait = st["next_check_at"] - time.time()
    # One 503 used to cost the whole day's single slot.
    check("retries in ~1h, not 24h", 3000 < wait < 3900, True)
    check("failure parked for the next session", st["notice"]["kind"], "failed")
    m.record_failure("HTTPError: 503 Backend.max_conn reached", cfg)
    check("backoff doubles", m.load_update_state()["retry_backoff"], 7200.0)
    hourly = dict(cfg, update=dict(cfg["update"], check_every_hours=1))
    m.record_failure("still failing", hourly)
    check("capped at the check interval", m.load_update_state()["retry_backoff"], 3600.0)

    # A check that succeeds has to retire the failure before it. It did not, so
    # one 503 kept nagging every new session until a release happened along.
    m.clear_notice()
    check("a good check retires the failure", m.load_update_state().get("notice"), None)
    m.record_notice("available", frm="1.6.0", to="1.7.0")
    m.clear_notice()
    check("but an `available` notice survives",
          (m.load_update_state().get("notice") or {}).get("kind"), "available")
    shutil.rmtree(tmp)


def test_update_rejects_bad_payloads():
    print("\n-- update: payload validation --")
    m, cfg, tmp, src = sandbox_update()
    man = src / "update-manifest.json"

    man.write_text(json.dumps([{"src": "bin/ctx.py", "dest": "root:bin/ctx.py",
                                "exec": True, "sha256": "0" * 64}]))
    m.save_update_state({})
    check("a sha256 that does not match", m.apply_update(cfg, force=True)["status"], "error")

    # A manifest is remote input, and remote input does not get to name ~/.zshrc.
    man.write_text(json.dumps([{"src": "bin/ctx.py", "dest": "root:../../.zshrc"}]))
    m.save_update_state({})
    res = m.apply_update(cfg, force=True)
    check("a destination outside the install", res["status"], "error")
    check("and it says which", "refused destination" in res["error"], True)

    man.write_text(json.dumps([{"src": "bin/ctx.py", "dest": "root:bin/ctx.py"}]))
    (src / "bin/ctx.py").write_text("VERSION = \"9.9.9\"\nprint('too short to be real')\n")
    m.save_update_state({})
    check("a truncated ctx.py", m.apply_update(cfg, force=True)["status"], "error")
    shutil.rmtree(tmp)


if __name__ == "__main__":
    test_writer_concurrency()
    test_handover_stamp_back_compat()
    test_latest_symlink_is_not_followed()
    test_update_refuses_local_edits()
    test_rollback_pins_from_applied_version()
    test_failure_backoff_and_notice_lifecycle()
    test_update_rejects_bad_payloads()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        sys.exit(1)
    print("all pass")
