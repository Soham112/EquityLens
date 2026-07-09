#!/bin/zsh
# Guarded daily-scan runner, invoked by launchd (com.equitylens.dailyscan).
#
# Fires at 9:35 Mon-Fri; launchd also re-fires it on wake if the Mac was asleep,
# and RunAtLoad fires it at login after a full shutdown. The guards below make
# all of those safe: it only ever runs the scan once per weekday, after 9:35.
#
# Logs: logs/daily_scan_{date}.log (gitignored)
# Manual force: delete data/daily_scan_{date}.json and re-run.

cd /Users/sohampatil/Documents/Projects/equitylens || exit 1

# Weekends: Sunday belongs to the weekly review; Saturday has no market data
DOW=$(date +%u)   # 1=Mon .. 7=Sun
[ "$DOW" -ge 6 ] && exit 0

# Not before 9:35 (RunAtLoad on an early login would otherwise scan pre-market)
NOW=$((10#$(date +%H%M)))
[ "$NOW" -lt 935 ] && exit 0

# Once per day: the scan writes this file when it completes
TODAY=$(date +%F)
[ -f "data/daily_scan_${TODAY}.json" ] && exit 0

mkdir -p logs
echo "=== launchd daily scan starting $(date) ===" >> "logs/daily_scan_${TODAY}.log"
PYTHONPATH=. .venv/bin/python workflows/daily_scan.py >> "logs/daily_scan_${TODAY}.log" 2>&1
echo "=== finished $(date) exit=$? ===" >> "logs/daily_scan_${TODAY}.log"
