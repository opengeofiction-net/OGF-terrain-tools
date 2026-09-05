#!/bin/bash
# Dump the recent changeset comments from the API database, for review.
#
# Replaces the psql one-liner in the ogf user's crontab on the Ubuntu 20.04
# server. TODO: replace the query and output path below with the ones from
# that crontab (migration notes, step 1) - this is a placeholder with the
# same shape.
#
# Connection details come from the environment - the systemd unit reads
# /etc/opengeofiction/db.env - or from the caller's PG* variables.
set -euo pipefail

DB_NAME=${DB_NAME:-ogfdevapi}
OUTPUT_DIR=${OUTPUT_DIR:-/opt/opengeofiction/ip-data}
DAYS=${DAYS:-7}

mkdir -p "${OUTPUT_DIR}"
psql -X -A -F '|' -d "${DB_NAME}" -o "${OUTPUT_DIR}/changeset-comments.txt" <<EOF
SELECT c.id, c.changeset_id, c.created_at, u.display_name, c.visible, c.body
  FROM changeset_comments c
  JOIN users u ON u.id = c.author_id
 WHERE c.created_at > now() - interval '${DAYS} days'
 ORDER BY c.created_at DESC;
EOF
