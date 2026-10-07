#!/bin/bash
# Runs tests/webkit_check.py in each combination of language, theme and layout, each against a new
# tests/walkthrough.py server, on the interface build the walkthrough driver last made and recorded
# for this checkout as it is now; the server must serve that build byte for byte, and must hold no
# socket but loopback ones while the check runs. Exits 1 if any of that fails.
#     tests/walkthrough_webkit.sh OUT
set -u
cd "$(dirname "$0")/.."
out=$1; mkdir -p "$out"; status=0
digest() { (cd frontend/dist && find . -type f | sed 's|^\./||' | LC_ALL=C sort | while read -r f; do printf '%s\0%s\n' "$f" "$(shasum -a 256 "$f" | cut -d' ' -f1)"; done | shasum -a 256 | cut -d' ' -f1); }
record=frontend/node_modules/.walkthrough-build.json
node --input-type=module - "$record" "$(digest)" <<'JS' || { echo "frontend/dist is not the recorded build of this checkout as it is now"; exit 1; }
// The same check as walkthrough_driver.mjs: the record's commit, uncommitted changes and digest.
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
const sh = (...args) => execFileSync('git', args, { encoding: 'utf8' }).trim();
const status = sh('status', '--porcelain', '--untracked-files=all');
const changes = status ? createHash('sha256').update(status + sh('diff', 'HEAD', '--binary')).digest('hex') : null;
const record = JSON.parse(readFileSync(process.argv[2], 'utf8'));
process.exit(record.commit === sh('rev-parse', 'HEAD') && record.changes === changes && record.distDigest === process.argv[3] ? 0 : 1);
JS
echo "$(git rev-parse HEAD) $(digest)" > "$out/provenance.txt"
for lang in en zh-CN; do for theme in light dark; do for layout in wide drawer; do
  log="$out/server-$lang-$theme-$layout.log"
  uv run --no-sync python tests/walkthrough.py > "$log" 2>&1 &
  for i in $(seq 1 100); do grep -q "open:" "$log" && break; sleep 0.2; done
  url=$(sed -n 's/^open: //p' "$log"); origin=${url%/#*}; port=${origin##*:}
  for i in $(seq 1 100); do lsof -t -iTCP:"$port" -sTCP:LISTEN >/dev/null && break; sleep 0.1; done
  pid=$(lsof -t -iTCP:"$port" -sTCP:LISTEN)
  for f in $(cd frontend/dist && find . -type f | sed 's|^\./||'); do
    p=$f; [ "$f" = index.html ] && p=""
    [ "$(curl -s "$origin/$p" | shasum -a 256 | cut -d' ' -f1)" = "$(shasum -a 256 "frontend/dist/$f" | cut -d' ' -f1)" ] || { echo "served $f differs"; status=1; }
  done
  uv run --no-sync python tests/webkit_check.py "$origin/#${url#*#}" --out "$out/$lang-$theme-$layout" --lang $lang --theme $theme --layout $layout &
  check=$!
  sleep 5; sockets=$(lsof -nP -a -p "$pid" -i | tail -n +2 | awk '{ n = split($9, ends, "->"); for (i = 1; i <= n; i++) if (ends[i] !~ /^(127\.0\.0\.1|\[::1\]):/) { print; next } }')
  [ -n "$sockets" ] && { echo "NON-LOOPBACK SOCKET: $sockets"; status=1; }
  wait $check || status=1
  kill "$pid"; rm -f "$log"
done; done; done
exit $status
