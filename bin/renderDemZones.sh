#!/bin/bash
#
# Re-render the footprint of each zone which fetchDemData.sh changed. Run as the
# ogf user, by tile-refresh-dem@<style>.service, after renderd has restarted -
# renderd holds the VRT open, so rendering before the restart would draw the old
# raster.
#
# /opt/opengeofiction/OGF-terrain-tools/bin/renderDemZones.sh <style>
#
# Zooms 0-5 are left to tile-render-lowzoom, which forces them twice daily.
# Override the rendered range with MIN_ZOOM and MAX_ZOOM.
#
# Above MAX_ZOOM the tiles are marked dirty rather than rendered, out to
# EXPIRE_MAX_ZOOM: mod_tile keeps serving what it has and re-renders in the
# background, which is right for a change to a background hillshade. Without
# this a zone's high zooms keep serving the old shading until something else
# happens to expire them.
#
# EXPIRE_MAX_ZOOM stops at 17 because the cost is geometric - covering a six by
# four degree zone takes 12,428 metatiles at z16, 49,710 at z17 and 795,364 at
# z19, and by then the hillshade is a faint background under everything else.
#
# changed-zones carries the footprint alongside each zone name, recorded by
# fetchDemData.sh. A zone which has been taken out of the render has no raster
# left to measure, and its tiles are precisely the ones which have to be redrawn.

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
EXPIRE_MAX_ZOOM=${EXPIRE_MAX_ZOOM:-17}

# Nothing changed, so nothing to re-render
[ -s ${CHANGED_ZONES} ] || exit 0

while read -r ZONE MINLON MINLAT MAXLON MAXLAT; do
	[ -n "${ZONE}" ] || continue

	if [ -z "${MAXLAT}" ]; then
		# a file written by the previous version, which listed names only
		TIF=${BASE}/shade/${ZONE}.tif
		[ -f ${TIF} ] || continue
		read -r MINLON MINLAT MAXLON MAXLAT < <(gdalinfo -json ${TIF} | python3 -c '
import json, sys
extent = json.load(sys.stdin)["wgs84Extent"]["coordinates"][0]
lons = [p[0] for p in extent]
lats = [p[1] for p in extent]
print(min(lons), min(lats), max(lons), max(lats))')
	fi

	echo "=========== re-rendering zone-${ZONE} z${MIN_ZOOM}-${MAX_ZOOM} ==========="
	echo "  ${MINLAT} ${MINLON} to ${MAXLAT} ${MAXLON}"
	render_list --all --force --map=${STYLE} \
		--min-zoom=${MIN_ZOOM} --max-zoom=${MAX_ZOOM} \
		-w ${MINLON} -W ${MAXLON} -g ${MINLAT} -G ${MAXLAT} \
		--max-load=6 --num-threads=2

	# and mark the zooms above dirty, one tile per metatile since that is the
	# unit mod_tile stores and expires
	EXPIRE_FROM=$((MAX_ZOOM + 1))
	if [ ${EXPIRE_FROM} -le ${EXPIRE_MAX_ZOOM} ]; then
		echo "  marking z${EXPIRE_FROM}-${EXPIRE_MAX_ZOOM} dirty"
		python3 - ${EXPIRE_FROM} ${EXPIRE_MAX_ZOOM} \
			${MINLON} ${MINLAT} ${MAXLON} ${MAXLAT} <<'PY' |
import math, sys
z0, z1 = int(sys.argv[1]), int(sys.argv[2])
w, s, e, n = (float(v) for v in sys.argv[3:7])
def xtile(lon, z):
    return int((lon + 180.0) / 360.0 * 2 ** z)
def ytile(lat, z):
    r = math.radians(max(min(lat, 85.05112878), -85.05112878))
    return int((1 - math.asinh(math.tan(r)) / math.pi) / 2 * 2 ** z)
for z in range(z0, z1 + 1):
    x0, x1 = xtile(w, z), xtile(e, z)
    y0, y1 = ytile(n, z), ytile(s, z)
    # step 8: one tile names the metatile which holds it
    for x in range(x0 - x0 % 8, x1 + 1, 8):
        for y in range(y0 - y0 % 8, y1 + 1, 8):
            print(f'{z}/{x}/{y}')
PY
		render_expired --map=${STYLE} --touch-from=${EXPIRE_FROM} \
			--min-zoom=${EXPIRE_FROM} --max-zoom=${EXPIRE_MAX_ZOOM} \
			--no-progress
	fi
done < ${CHANGED_ZONES}

rm -f ${CHANGED_ZONES}
echo "=========== done ==========="
