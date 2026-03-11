#!/usr/bin/env bash
# ApplyPilot automated scan — discover, enrich, score
set -euo pipefail

LOGDIR="/home/pi/applypilot/logs"
mkdir -p "$LOGDIR"
LOGFILE="$LOGDIR/scan-$(date +%Y%m%d-%H%M%S).log"

echo "=== ApplyPilot scan started at $(date) ===" | tee "$LOGFILE"

cd /home/pi/applypilot
.venv/bin/applypilot run discover enrich score 2>&1 | tee -a "$LOGFILE"

echo "=== Scan finished at $(date) ===" | tee -a "$LOGFILE"

# Keep only last 30 log files
ls -t "$LOGDIR"/scan-*.log 2>/dev/null | tail -n +31 | xargs -r rm --
