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
# Above MAX_ZOOM the cached tiles over the changed zones are marked dirty
# rather than rendered: mod_tile keeps serving what it has and re-renders
# behind the reader, which is right for a change to a background hillshade.
#
# Without this they keep the old shading for up to three days. With no
# planet-import-complete file mod_tile makes a timestamp up - now minus three
# days, from getPlanetTime() in store_file.c - so a tile is treated as expired
# only once it is that old. Which leaves the recently rendered tiles stale
# longest, and those are the ones people are looking at.
#
# demExpireTiles.py sweeps the cache for the tiles which exist, in one pass for
# every zone, so this costs a readdir rather than the 795,364 stat calls that
# enumerating one zone's z19 footprint would take.
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
TOOLS=${TOOLS:-/opt/opengeofiction/OGF-terrain-tools}
CHANGED_ZONES=${BASE}/changed-zones
MIN_ZOOM=${MIN_ZOOM:-6}
MAX_ZOOM=${MAX_ZOOM:-12}
EXPIRE=${EXPIRE:-yes}

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

done < ${CHANGED_ZONES}

# and the zooms above, in one sweep of the cache
if [ "${EXPIRE}" = "yes" ]; then
	EXPIRE_FROM=$((MAX_ZOOM + 1))
	echo "=========== marking z${EXPIRE_FROM} and above dirty ==========="
	${TOOLS}/bin/demExpireTiles.py ${CHANGED_ZONES} ${STYLE} ${EXPIRE_FROM} |
		render_expired --map=${STYLE} --touch-from=${EXPIRE_FROM} \
			--min-zoom=${EXPIRE_FROM} --no-progress
fi

rm -f ${CHANGED_ZONES}
echo "=========== done ==========="
