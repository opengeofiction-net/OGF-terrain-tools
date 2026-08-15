#!/bin/bash
#
# Re-render the footprint of each zone which fetchDemData.sh changed. Run as the
# ogf user, by tile-refresh-dem@<style>.service, after renderd has restarted -
# renderd holds the VRT open, so rendering before the restart would draw the old
# raster.
#
# /opt/opengeofiction/OGF-terrain-tools/bin/renderDemZones.sh <style>
#
# Zooms 0-5 are left to tile-render-lowzoom, which forces them twice daily, and
# the high zooms to demand. Override with MIN_ZOOM and MAX_ZOOM.

set -e

if [ $# -ne 1 ]; then
	echo "Usage: $0 <style>" >&2
	exit 1
fi
STYLE=$1

BASE=/opt/opengeofiction/dem
CHANGED_ZONES=${BASE}/changed-zones
MIN_ZOOM=${MIN_ZOOM:-6}
MAX_ZOOM=${MAX_ZOOM:-12}

# Nothing changed, so nothing to re-render
[ -s ${CHANGED_ZONES} ] || exit 0

while read -r ZONE; do
	TIF=${BASE}/shade/${ZONE}.tif
	[ -f ${TIF} ] || continue

	# The zone footprint, in lat/lon, from the raster itself
	read -r MINLON MINLAT MAXLON MAXLAT < <(gdalinfo -json ${TIF} | python3 -c '
import json, sys
extent = json.load(sys.stdin)["wgs84Extent"]["coordinates"][0]
lons = [p[0] for p in extent]
lats = [p[1] for p in extent]
print(min(lons), min(lats), max(lons), max(lats))')

	echo "=========== re-rendering zone-${ZONE} z${MIN_ZOOM}-${MAX_ZOOM} ==========="
	echo "  ${MINLAT} ${MINLON} to ${MAXLAT} ${MAXLON}"
	render_list --all --force --map=${STYLE} \
		--min-zoom=${MIN_ZOOM} --max-zoom=${MAX_ZOOM} \
		-w ${MINLON} -W ${MAXLON} -g ${MINLAT} -G ${MAXLAT} \
		--max-load=6 --num-threads=2
done < ${CHANGED_ZONES}

rm -f ${CHANGED_ZONES}
echo "=========== done ==========="
