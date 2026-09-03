#!/usr/bin/env bash
# Install the Claude Code context guard + handover skill on this machine.
#
#   git clone https://github.com/teminali/claude-context-guard
#   cd claude-context-guard && ./install.sh
#
# Idempotent: safe to re-run after a `git pull`. It never overwrites your
# config.json, your handover docs, or your per-session state.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HOME/.claude/handover"
SKILL="$HOME/.claude/skills/handover"
AGENTS="$HOME/.claude/agents"

command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
[ -f "$SRC/bin/ctx.py" ] || { echo "missing $SRC/bin/ctx.py - run this from the checkout"; exit 1; }

# Copy the tool in, unless this checkout *is* the install (developing in place).
if [ "$SRC" != "$ROOT" ]; then
  mkdir -p "$ROOT/bin" "$SKILL" "$AGENTS"
  cp "$SRC/bin/ctx.py"                "$ROOT/bin/ctx.py"
  cp "$SRC/skills/handover/SKILL.md"  "$SKILL/SKILL.md"
  cp "$SRC/agents/"*.md               "$AGENTS/"
  # Your thresholds and share folder are yours: seed the config once, never clobber it.
  [ -f "$ROOT/config.json" ] || cp "$SRC/config.sample.json" "$ROOT/config.json"
  echo "installed  -> $ROOT/bin/ctx.py, $SKILL/SKILL.md, $AGENTS/{verify-pickup,handover-staleness}.md"
fi

chmod +x "$ROOT/bin/ctx.py"
python3 "$ROOT/bin/ctx.py" install "$@"

# Point this machine at the same shared handover folder, if iCloud Drive is present.
# Two Macs sharing this folder can hand work to each other; skip it and nothing breaks.
ICLOUD="$HOME/Library/Mobile Documents/com~apple~CloudDocs/claude-handovers"
if [ -d "$(dirname "$ICLOUD")" ]; then
  mkdir -p "$ICLOUD"
  python3 - "$ICLOUD" <<'PY'
import json, pathlib, sys
p = pathlib.Path.home()/".claude/handover/config.json"
c = json.loads(p.read_text()) if p.exists() else {}
if not c.get("share_dir"):
    c["share_dir"] = sys.argv[1]
    p.write_text(json.dumps(c, indent=2)+"\n")
    print("share_dir ->", sys.argv[1])
PY
fi

echo
python3 "$ROOT/bin/ctx.py" doctor || true
echo
echo "Done. Open /hooks once (or restart Claude Code) so the hooks load."
