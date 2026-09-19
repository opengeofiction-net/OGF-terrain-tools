#!/bin/bash
# Top 25 client IPs across yesterday's and today's access logs for the site
# and for data.opengeofiction.net. Daily from ogfutil-topIps.timer, output to
# the journal; was a cat|awk|sort one-liner in the ogf crontab, to cron mail.
set -euo pipefail

SITE=${SITE:-/var/www/html/opengeofiction.net/log}
DATA=${DATA:-/var/www/html/data.opengeofiction.net/logs}
TOP=${TOP:-25}

# only the logs that exist: a fresh server has no rotated .1 yet, and under
# pipefail a missing file would fail the unit despite the output being right
logs=()
for f in "${SITE}/access.log.1" "${SITE}/access.log" "${DATA}/access.log.1" "${DATA}/access.log"; do
  [ -r "$f" ] && logs+=("$f")
done
[ ${#logs[@]} -gt 0 ] || { echo "no access logs found under ${SITE} or ${DATA}" >&2; exit 1; }
cat "${logs[@]}" | awk '{print $1}' | sort | uniq -c | sort -nr | head -n "${TOP}"
