#!/bin/bash
# ogf set read-only
# Place the site in api_readonly status: the web site stays up, edits are
# refused by both Rails and CGIMap. Undo with ogf-set-online.sh.
set -euo pipefail
RAILS=/var/www/html/opengeofiction.net
CGIMAP_ENV=/opt/opengeofiction/openstreetmap-cgimap/etc/cgimap.env

# Rails reads status from settings.local.yml - settings.yml is upstream's and
# is never edited
sed -i 's|^status: "online"|status: "api_readonly"|' ${RAILS}/config/settings.local.yml
touch ${RAILS}/tmp/restart.txt

# CGIMap does not read the Rails setting
grep -q '^CGIMAP_DISABLE_API_WRITE=' ${CGIMAP_ENV} || echo 'CGIMAP_DISABLE_API_WRITE=true' >> ${CGIMAP_ENV}
sudo systemctl restart cgimap

grep '^status:' ${RAILS}/config/settings.local.yml
