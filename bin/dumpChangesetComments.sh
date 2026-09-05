#!/bin/bash
# Dump four weeks of visible changeset comments as JSON, for OGF-Patrol and
# the review tools, to data.opengeofiction.net. Hourly from
# ogfutil-changesetComments.timer; was a psql one-liner in the ogf crontab.
set -euo pipefail

DB_NAME=${DB_NAME:-ogfdevapi}
OUTPUT=${OUTPUT:-/var/www/html/data.opengeofiction.net/public_html/changeset-comments/recent.json}

mkdir -p "$(dirname "${OUTPUT}")"
psql --dbname="${DB_NAME}" --output="${OUTPUT}.tmp" --tuples-only --command="
SELECT ARRAY_TO_JSON(ARRAY_AGG(ROW_TO_JSON(r)))
  FROM (SELECT cc.created_at,
               CONCAT('https://opengeofiction.net/changeset/', cc.changeset_id, '#c', cc.id) AS url,
               u.display_name AS user,
               LEFT(cc.body, 60) AS comment
          FROM changeset_comments cc, users u
         WHERE cc.created_at > NOW() - INTERVAL '4 weeks'
           AND cc.visible = true
           AND cc.author_id = u.id
         ORDER BY created_at DESC) r;"
mv "${OUTPUT}.tmp" "${OUTPUT}"
