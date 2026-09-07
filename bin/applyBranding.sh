#!/bin/bash
# Copy the OGF favicons and logo over the upstream files in an
# openstreetmap-website checkout, from the util repo's logo/favicons set.
# Run after checkout and before assets:precompile; idempotent. Like
# applyLocaleSubs.py it leaves the working tree dirty on purpose - these
# files are never committed to the ogf branch.
#
# Usage: applyBranding.sh <rails_root> [util_checkout]
#        default util checkout /opt/opengeofiction/util (cloned if absent)
set -euo pipefail

RAILS=${1:?rails root}
UTIL=${2:-/opt/opengeofiction/util}
SRC=${UTIL}/logo/favicons

if [ ! -d "${UTIL}/.git" ]; then
	git clone -q https://github.com/opengeofiction-net/util.git "${UTIL}"
else
	git -C "${UTIL}" pull -q --ff-only
fi

cp "${SRC}"/android-chrome-*.png "${SRC}"/apple-touch-icon*.png "${SRC}"/favicon*.png "${SRC}"/favicon.ico "${SRC}"/mstile-*.png "${RAILS}/app/assets/favicons/"
cp "${SRC}"/osm_logo.svg "${SRC}"/osm_logo_*.png "${RAILS}/app/assets/images/"

# the two templates stay upstream's, with the app name and tile colour changed
sed -i 's/"name": "OpenStreetMap"/"name": "OpenGeofiction"/' "${RAILS}/app/assets/favicons/manifest.json.erb"
sed -i 's|<TileColor>#[0-9a-fA-F]*</TileColor>|<TileColor>#A45A52</TileColor>|' "${RAILS}/app/assets/favicons/browserconfig.xml.erb"
sed -i 's|:color => "#[0-9a-fA-F]*"|:color => "#A45A52"|' "${RAILS}/app/views/layouts/_meta.html.erb"

git -C "${RAILS}" status --short app/assets/favicons app/assets/images app/views/layouts/_meta.html.erb | wc -l | xargs echo "branding: files changed:"
