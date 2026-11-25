#!/usr/bin/env bash
# One-command start: preflight, then the supervisor under caffeinate, auto-restarting on crash.
# Stop: touch data/STOP  (or Ctrl+C in the foreground).   Pause posting only: ./ctl pause
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:$PATH" PYTHONUNBUFFERED=1

fail() { echo "PREFLIGHT FAIL: $*"; exit 1; }
echo "== preflight"
command -v streamlink >/dev/null || fail "streamlink missing"
ffmpeg -hide_banner -filters 2>/dev/null | grep -q ' ass ' || fail "ffmpeg lacks libass"
ffmpeg -hide_banner -encoders 2>/dev/null | grep -q h264_videotoolbox || fail "ffmpeg lacks h264_videotoolbox"
free_gb=$(df -g . | awk 'NR==2{print $4}'); [ "$free_gb" -ge 15 ] || fail "only ${free_gb} GB free"
timeout 120 uv run pytest -q tests >/dev/null || echo "WARN: tests failed or timed out"
timeout 90 ./ctl check >/dev/null || echo "WARN: ./ctl check failed or timed out - starting anyway"
echo "preflight ok (disk ${free_gb} GB free)"
rm -f data/STOP
restarts=0
while [ ! -f data/STOP ]; do
  echo "== $(date '+%H:%M:%S') starting clipbot (restart #$restarts)"
  caffeinate -dimsu uv run python -m clipbot.main
  code=$?
  [ -f data/STOP ] && break
  restarts=$((restarts+1))
  echo "== clipbot exited ($code) - restarting in 10s"
  [ $restarts -ge 3 ] && curl -s -d "ClipBot crashed and restarted ($restarts times) - check logs/run.out" "ntfy.sh/$(grep ^NTFY_TOPIC .env | cut -d= -f2)" >/dev/null
  sleep 10
done
echo "== stopped"
