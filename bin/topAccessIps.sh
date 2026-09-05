#!/bin/bash
# Summarise the top client IPs in the API access log.
#
# Replaces the grep/sort/uniq one-liners in the ogf user's crontab on the
# Ubuntu 20.04 server. TODO: replace the patterns and output paths below with
# the ones from that crontab (migration notes, step 1) - this is a placeholder
# with the same shape.
set -euo pipefail

LOG=${LOG:-/var/www/html/opengeofiction.net/log/access.log}
OUTPUT_DIR=${OUTPUT_DIR:-/opt/opengeofiction/ip-data}
TOP=${TOP:-50}

mkdir -p "${OUTPUT_DIR}"
day=$(date -u +%Y%m%d)

# all requests
awk '{print $1}' "${LOG}" | sort | uniq -c | sort -rn | head -n "${TOP}" > "${OUTPUT_DIR}/top-ips-${day}.txt"
# element page views, the scraper target
grep -E '"GET /(node|way|relation|changeset)/[0-9]+' "${LOG}" | awk '{print $1}' | sort | uniq -c | sort -rn | head -n "${TOP}" > "${OUTPUT_DIR}/top-ips-elements-${day}.txt"
