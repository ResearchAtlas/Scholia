#!/bin/bash
# Runs tests/webkit_check.py in each combination of language, theme and layout, each against a new
# tests/walkthrough.py server, on the interface build the walkthrough driver last made and recorded
# for this checkout as it is now; the server must serve that build byte for byte, and the server, the
# checker and the WebKit processes started for it must hold no socket but loopback ones while the
# check runs. Exits 1 if any of that fails.
#     tests/walkthrough_webkit.sh OUT
set -u
cd "$(dirname "$0")/.."
out=$1; status=0
# Every file in OUT must come from this run.
if [ -e "$out" ] && [ -n "$(ls -A "$out")" ]; then echo "$out is not empty: give each run a new folder"; exit 1; fi
mkdir -p "$out"
digest() { (cd frontend/dist && find . -type f | sed 's|^\./||' | LC_ALL=C sort | while read -r f; do printf '%s\0%s\n' "$f" "$(shasum -a 256 "$f" | cut -d' ' -f1)"; done | shasum -a 256 | cut -d' ' -f1); }
record=frontend/node_modules/.walkthrough-build.json
node --input-type=module - "$record" "$(digest)" <<'JS' || { echo "frontend/dist is not the recorded build of this checkout as it is now"; exit 1; }
// The same check as walkthrough_driver.mjs: the record's commit, uncommitted changes and digest.
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
const sh = (...args) => execFileSync('git', args, { encoding: 'utf8' }).trim();
const status = sh('status', '--porcelain', '--untracked-files=all');
const untracked = execFileSync('git', ['ls-files', '--others', '--exclude-standard', '-z'], { encoding: 'utf8' }).split('\0').filter(Boolean)
  .map((path) => `${path}\0${createHash('sha256').update(readFileSync(path)).digest('hex')}`).join('\n');
const changes = status ? createHash('sha256').update(`${status}\n${sh('diff', 'HEAD', '--binary')}\n${untracked}`).digest('hex') : null;
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
  # pgrep exits 1 when nothing matches; any other status is a failed discovery, never "none".
  discover() { local found; found=$(pgrep "$@"); local st=$?; if [ $st -gt 1 ]; then echo "pgrep $* failed ($st)" >&2; return 2; fi; echo "$found"; }
  webkit_before=$(discover -f 'com\.apple\.WebKit') || { echo "process discovery failed"; status=1; }
  uv run --no-sync python tests/webkit_check.py "$origin/#${url#*#}" --out "$out/$lang-$theme-$layout" --lang $lang --theme $theme --layout $layout &
  check=$!
  sleep 5
  # Every process this check launched: the server, the checker and its children, and the WebKit
  # processes started since (WebKit's content and networking services, which launchd starts).
  webkit_after=$(discover -f 'com\.apple\.WebKit') || { echo "process discovery failed"; status=1; }
  children=$(discover -P "$check") || { echo "process discovery failed"; status=1; }
  webkit_new=$(comm -13 <(echo "$webkit_before" | sort) <(echo "$webkit_after" | sort))
  pids=$(echo "$pid $check $children $webkit_new" | tr -s ' \n' ',' | sed 's/^,//; s/,$//')
  lsof_out=$(lsof -nP -a -p "$pids" -i 2>"$out/lsof.err"); lsof_status=$?
  if { [ $lsof_status -ne 0 ] && [ $lsof_status -ne 1 ]; } || [ -s "$out/lsof.err" ] \
     || { [ -n "$lsof_out" ] && ! head -1 <<<"$lsof_out" | grep -q '^COMMAND'; }; then
    echo "socket check failed for $lang-$theme-$layout: $(cat "$out/lsof.err")"; status=1
  else
    sockets=$(tail -n +2 <<<"$lsof_out" | awk '{ n = split($9, ends, "->"); for (i = 1; i <= n; i++) if (ends[i] !~ /^(127\.0\.0\.1|\[::1\]):/) { print; next } }')
    [ -n "$sockets" ] && { echo "NON-LOOPBACK SOCKET: $sockets"; status=1; }
  fi
  echo "$lang-$theme-$layout processes checked: $pids" >> "$out/sockets.txt"
  rm -f "$out/lsof.err"
  wait $check || status=1
  kill "$pid"; rm -f "$log"
done; done; done
exit $status
