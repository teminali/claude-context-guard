#!/usr/bin/env bash
# Publish a version. Everyone's install picks it up within 24h.
#
#   ./release.sh 1.6.0 "what changed, in one line"
#
# Bumps VERSION and UPDATE_NOTE in bin/ctx.py, writes the CHANGELOG entry,
# commits, tags v<version>, and pushes both. The note is what every session on
# every machine is shown when the update lands, so write it for them.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

VER="${1:-}"; NOTE="${2:-}"
[ -n "$VER" ] && [ -n "$NOTE" ] || { echo "usage: ./release.sh <version> \"one-line note\""; exit 1; }
[[ "$VER" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "version must be N.N.N"; exit 1; }
case "$NOTE" in *'"'*) echo 'the note cannot contain a double quote'; exit 1;; esac

CUR="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' bin/ctx.py | head -1)"
python3 - "$CUR" "$VER" <<'PY'
import sys
def v(s): return tuple(int(x) for x in s.split("."))
if v(sys.argv[2]) <= v(sys.argv[1]):
    sys.exit(f"{sys.argv[2]} is not newer than the published {sys.argv[1]} - "
             "clients compare numerically and would ignore it")
PY
[ -z "$(git status --porcelain)" ] || { echo "working tree is dirty - commit or stash first"; exit 1; }
[ "$(git rev-parse --abbrev-ref HEAD)" = "main" ] || { echo "release from main"; exit 1; }

python3 - "$VER" "$NOTE" <<'PY'
import pathlib, re, sys, datetime
ver, note = sys.argv[1], sys.argv[2]
p = pathlib.Path("bin/ctx.py"); s = p.read_text()
s = re.sub(r'^VERSION = ".*"$', f'VERSION = "{ver}"', s, count=1, flags=re.M)
s = re.sub(r'^UPDATE_NOTE = ".*"$', f'UPDATE_NOTE = "{note}"', s, count=1, flags=re.M)
p.write_text(s)
c = pathlib.Path("CHANGELOG.md")
head = "# Changelog\n\nEvery entry's one-line note is what installed sessions are shown when they update.\n"
body = c.read_text() if c.exists() else head
day = datetime.date.today().isoformat()
c.write_text(body.replace(head, head + f"\n## {ver} - {day}\n\n{note}\n", 1)
             if head in body else f"{head}\n## {ver} - {day}\n\n{note}\n\n" + body)
PY

git add bin/ctx.py CHANGELOG.md
git commit -m "release: $VER - $NOTE"
git tag -a "v$VER" -m "$NOTE"
git push origin main "v$VER"
echo
echo "published $CUR -> $VER"
echo "every install checks within 24h; to pull it now: python3 ~/.claude/handover/bin/ctx.py update"
