#!/bin/bash
# ogf set online
# Return the site to online status from api_readonly - see ogf-set-read-only.sh
set -euo pipefail
RAILS=/var/www/html/opengeofiction.net
CGIMAP_ENV=/opt/opengeofiction/openstreetmap-cgimap/etc/cgimap.env

sed -i 's|^status: "api_readonly"|status: "online"|' ${RAILS}/config/settings.local.yml
passenger-config restart-app ${RAILS} >/dev/null

sed -i '/^CGIMAP_DISABLE_API_WRITE=/d' ${CGIMAP_ENV}
sudo systemctl restart cgimap

grep '^status:' ${RAILS}/config/settings.local.yml
