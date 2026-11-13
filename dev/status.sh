#!/usr/bin/env bash
# What's running, and is it still moving? Works in the devcontainer or WSL, with or without Claude.
#
#   bash dev/status.sh            # once
#   watch -n 30 bash dev/status.sh
#
# It shows the status of each orchestrated run in .runs/*/status.md, the long jobs running now
# with their elapsed time, and the last line of each progress file written in the last day.
# A progress file that hasn't changed for STALE_MIN minutes (default 5) while a job runs is
# flagged: either one step is slow, or something is stuck, and it's worth a look.
cd "$(dirname "$0")/.." || exit 1
STALE_MIN=${STALE_MIN:-5}
now=$(date +%s)

echo "== Orchestrated runs"
found=0
for status in .runs/*/status.md; do
    [ -f "$status" ] || continue
    found=1
    age=$(( (now - $(stat -c %Y "$status")) / 60 ))
    echo "-- $status (updated $age min ago)"
    head -n 12 "$status" | sed 's/^/   /'
done
[ $found = 1 ] || echo "   none"

echo "== Long jobs running"
pattern='probe\.py|run_perf\.py|ab\.py|checksum\.py|profile_diff\.py|rig\.py|seed\.py|unittest'
jobs=$(ps -eo pid=,etimes=,args= | grep -E "$pattern" | grep -vE 'grep|status\.sh')
if [ -z "$jobs" ]; then
    echo "   none"
else
    echo "$jobs" | while read -r pid secs args; do
        printf '   pid %-7s %4d min  %s\n' "$pid" $((secs / 60)) "$(echo "$args" | cut -c1-110)"
    done
fi

echo "== Progress files (last day)"
found=0
while IFS= read -r file; do
    found=1
    age=$(( (now - $(stat -c %Y "$file")) / 60 ))
    flag=""
    if [ -n "$jobs" ] && [ "$age" -ge "$STALE_MIN" ]; then
        flag="  <-- no progress for $age min"
    fi
    echo "   $file ($age min ago)$flag"
    echo "     $(tail -n 1 "$file" | cut -c1-140)"
done < <(find .runs /tmp -maxdepth 4 \( -name 'progress*.log' -o -name 'perf*.log' \) -mmin -1440 \
    -exec realpath {} \; 2>/dev/null | sort -u)
[ $found = 1 ] || echo "   none"
