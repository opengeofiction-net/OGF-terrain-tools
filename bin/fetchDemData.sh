#!/bin/bash
#
# Fetch the OGF elevation data used by the DEM layers, and load it.
# Run as the ogf user, by tile-refresh-dem@<style>.timer, or by hand.
#
# The zones are produced on the utility server - see Admin:Elevation process
#
# /opt/opengeofiction/OGF-terrain-tools/bin/fetchDemData.sh <style> [zone ...]
#
# With no zones named, the zones to load are whatever dem/active-zones.txt says.
# That list is about rendering, not publishing: a zone can be published and
# downloadable and still be left out of it, so what is on the server is not the
# same question as what belongs on the map. Naming zones does just those, and
# skips the removal pass.
#
# Downloads only happen when the published copy is newer, and the VRT and the
# database are only rebuilt when something has actually changed.
#
# The zones which changed are left in dem/changed-zones as
# "<zone> <minlon> <minlat> <maxlon> <maxlat>", for renderDemZones.sh. The
# footprint is recorded here rather than derived there because a zone which has
# been removed no longer has a raster to derive it from, and its tiles are
# exactly the ones which have to be redrawn.
#
# Note the data is shared, not per style: one dem directory and one contours
# database, whatever %i says. Only enable the timer for one style.

set -e

if [ $# -lt 1 ]; then
	echo "Usage: $0 <style> [zone ...]" >&2
	exit 1
fi
STYLE=$1
shift

# Overridable so the fetch and removal passes can be exercised against a copy
# of the published tree, off the tile server
BASE=${BASE:-/opt/opengeofiction/dem}
SRC=${SRC:-https://data.opengeofiction.net/dem}
# Which hillshade this style wants. z2 is the softer one, which is what cyclogf
# reads; the topo layer uses z5. Same DEM, two strengths
ZFACTOR=${ZFACTOR:-z2}
TOOLS=${TOOLS:-/opt/opengeofiction/OGF-terrain-tools}
OSM2PGSQL_STYLE=${TOOLS}/etc/cyclogf_contours.style
RAMP=${RAMP:-/opt/opengeofiction/map-styles/${STYLE}/dem/shade.ramp}
CHANGED_ZONES=${BASE}/changed-zones
DB=contours

mkdir -p ${BASE}/hillshade ${BASE}/shade ${BASE}/contours

# One run at a time. The timer and a manual run must not both be rebuilding the
# VRT, or reloading the database, at once
exec 9>${BASE}/.lock
if ! flock -n 9; then
	echo "another run is in progress, giving up" >&2
	exit 1
fi

[ -f ${RAMP} ] || { echo "no shade ramp at ${RAMP}" >&2; exit 1; }

# Fetch to a temporary file and only move it into place once it is there. curl
# truncates its output on failure, so fetching straight to the destination would
# leave an empty file behind when the server is unreachable
# Returns: 0 fetched something new, 1 unchanged, 2 failed
fetch() {
	local url=$1 dest=$2 tmp=$2.tmp
	rm -f ${tmp}
	if ! curl -sS --fail --retry 3 --retry-delay 5 --remote-time -o ${tmp} \
		$([ -f ${dest} ] && echo "-z ${dest}") ${url}; then
		rm -f ${tmp}
		echo "WARNING: could not fetch ${url}" >&2
		return 2
	fi
	if [ -s ${tmp} ]; then
		mv ${tmp} ${dest}
		return 0
	fi
	rm -f ${tmp}    # not modified, curl wrote nothing
	return 1
}

# The footprint of a zone's raster, in lat/lon
footprint() {
	gdalinfo -json $1 | python3 -c '
import json, sys
extent = json.load(sys.stdin)["wgs84Extent"]["coordinates"][0]
lons = [p[0] for p in extent]
lats = [p[1] for p in extent]
print(min(lons), min(lats), max(lons), max(lats))'
}

# The zone list. Either the arguments, or the manifest - and the manifest is
# fetched rather than assumed: if it cannot be read, nothing is done, because
# every zone would otherwise look like it had been removed
REMOVALS=yes
if [ $# -gt 0 ]; then
	ZONES="$@"
	REMOVALS=
else
	MANIFEST=${BASE}/active-zones.txt
	if ! fetch ${SRC}/active-zones.txt ${MANIFEST} && [ ! -s ${MANIFEST} ]; then
		echo "could not read ${SRC}/active-zones.txt, nothing to do" >&2
		exit 1
	fi
	ZONES=$(grep -v '^#' ${MANIFEST} | tr -d ' \t' | grep -v '^$' | sort -u)
	[ -n "${ZONES}" ] || { echo "the manifest lists no zones, nothing to do" >&2; exit 1; }
fi
echo "zones: $(echo ${ZONES} | tr '\n' ' ')"

CHANGED=""
FAILED=""
: > ${CHANGED_ZONES}.new

for ZONE in ${ZONES}; do
	echo "=========== zone-${ZONE} ==========="
	ZONE_CHANGED=""

	TIF=${BASE}/hillshade/${ZONE}.tif
	rc=0; fetch ${SRC}/${ZONE}/hillshade-${ZFACTOR}.tif ${TIF} || rc=$?
	[ ${rc} -eq 0 ] && ZONE_CHANGED=yes
	[ ${rc} -eq 2 ] && { FAILED="${FAILED} ${ZONE}"; continue; }

	PBF=${BASE}/contours/contours-${ZONE}.osm.pbf
	rc=0; fetch ${SRC}/${ZONE}/contours-${ZONE}.osm.pbf ${PBF} || rc=$?
	[ ${rc} -eq 0 ] && ZONE_CHANGED=yes
	[ ${rc} -eq 2 ] && { FAILED="${FAILED} ${ZONE}"; continue; }

	# The greyscale hillshade cannot be used directly. gdaldem gives flat ground
	# a valid mid grey, around 180, not nodata - for gobras that is 98% of the
	# raster - so compositing it would darken everything inside the zone's
	# rectangle, sea included, with a hard edge at the boundary. The style's
	# ramp turns it into RGBA, flat ground fully transparent and only slopes
	# shaded, which is also a good deal smaller.
	#
	# Tiled, not the gdal default of one scanline per block: rendering reads
	# small windows at random, and a striped file has to inflate a full strip
	# for each
	SHADE=${BASE}/shade/${ZONE}.tif
	if [ ! -f ${SHADE} ] || [ ${TIF} -nt ${SHADE} ] || [ ${RAMP} -nt ${SHADE} ]; then
		echo "applying the shade ramp"
		gdaldem color-relief -alpha -q ${TIF} ${RAMP} ${SHADE} \
			-co TILED=YES -co BLOCKXSIZE=256 -co BLOCKYSIZE=256 \
			-co COMPRESS=DEFLATE -co PREDICTOR=2
		ZONE_CHANGED=yes
	fi

	if [ -n "${ZONE_CHANGED}" ]; then
		CHANGED="${CHANGED} ${ZONE}"
		echo "${ZONE} $(footprint ${SHADE})" >> ${CHANGED_ZONES}.new
	fi
done

[ -n "${FAILED}" ] && echo "WARNING: zones which could not be fetched:${FAILED}" >&2

# ----------- zones which have left the manifest -----------------
# A zone taken out of the render has to be taken off the map, not just stopped
# from updating: its rasters and its contours go, and its footprint is redrawn.
# Skipped when zones were named on the command line, where the argument list says
# nothing about what should no longer be here.
REMOVED=""
if [ -n "${REMOVALS}" ]; then
	for SHADE in ${BASE}/shade/*.tif; do
		[ -e "${SHADE}" ] || continue
		ZONE=$(basename ${SHADE} .tif)
		grep -qxF "${ZONE}" <<<"${ZONES}" && continue

		echo "=========== zone-${ZONE} has left the manifest ==========="
		# the footprint has to be taken before the raster goes
		echo "${ZONE} $(footprint ${SHADE})" >> ${CHANGED_ZONES}.new
		rm -f ${SHADE} ${BASE}/hillshade/${ZONE}.tif \
			${BASE}/contours/contours-${ZONE}.osm.pbf \
			${BASE}/sorted/${ZONE}.osm.pbf
		REMOVED="${REMOVED} ${ZONE}"
		CHANGED="${CHANGED} ${ZONE}"
	done
fi
[ -n "${REMOVED}" ] && echo "removed:${REMOVED}"

# Normally there is nothing new, and the rest is not worth doing. The existing
# data is left exactly as it is
if [ -z "${CHANGED}" ]; then
	echo "=========== no zones changed ==========="
	rm -f ${CHANGED_ZONES} ${CHANGED_ZONES}.new
	exit 0
fi
echo "=========== changed:${CHANGED} ==========="

# ----------- hillshade -----------------
# One VRT over every zone held locally, with overviews so the low zooms do not
# have to read the full resolution rasters.
#
# The .ovr has to go first. gdalbuildvrt -overwrite replaces the VRT but leaves
# the overviews alone, and gdaladdo then tries to update them - if the band
# count has changed since, that is an "Illegal band" error and a segfault.
#
# COMPRESS_OVERVIEW is not optional either. The mosaic spans the bounding box of
# every zone, which is most of the planet, and overviews are dense - the empty
# space between zones is written out in full. Two zones alone give an 88MB .ovr,
# and 476KB once the nodata compresses away
echo "=========== building shade.vrt ==========="
rm -f ${BASE}/shade.vrt.ovr
gdalbuildvrt -overwrite ${BASE}/shade.vrt ${BASE}/shade/*.tif
gdaladdo -r average --config COMPRESS_OVERVIEW DEFLATE \
	${BASE}/shade.vrt 2 4 8 16 32 64 128 256 512

# ----------- contours -----------------
# Loaded from scratch, there being no incremental update to do, and no --slim for
# the same reason, which keeps the database a good deal smaller.
#
# The published files come out of demContoursToOsm.py sorted by type and id, so
# there is nothing to sort here. Older files, from phyghtmap, interleaved node
# and way blocks and needed sorting first, so it is checked rather than assumed -
# osmium merge would otherwise fail halfway through the load
echo "=========== loading contours ==========="
psql -lqt | cut -d\| -f1 | grep -qw ${DB} || createdb -E UTF8 ${DB}
psql -d ${DB} -qc "CREATE EXTENSION IF NOT EXISTS postgis"
psql -d ${DB} -qc "DROP VIEW IF EXISTS contours"

MERGE_FILES=""
mkdir -p ${BASE}/sorted
for PBF in ${BASE}/contours/*.osm.pbf; do
	[ -e "${PBF}" ] || continue
	ZONE=$(basename ${PBF} .osm.pbf); ZONE=${ZONE#contours-}
	if osmium fileinfo -e ${PBF} | grep -q 'Objects ordered (by type and id): yes'; then
		MERGE_FILES="${MERGE_FILES} ${PBF}"
		continue
	fi
	SORTED=${BASE}/sorted/${ZONE}.osm.pbf
	if [ ! -f ${SORTED} ] || [ ${PBF} -nt ${SORTED} ]; then
		echo "  ${ZONE}: not ordered, sorting"
		osmium sort ${PBF} --overwrite -o ${SORTED}
	fi
	MERGE_FILES="${MERGE_FILES} ${SORTED}"
done

# merge, not cat: osm2pgsql needs the input ordered, and cat concatenates, so
# the second zone's nodes would follow the first zone's ways. The merge streams
# the per-zone files, and each zone has its own id block, so there is nothing to
# deduplicate
osmium merge ${MERGE_FILES} --overwrite -o ${BASE}/contours-all.osm.pbf
osm2pgsql --database ${DB} --create --style ${OSM2PGSQL_STYLE} \
	--number-processes=4 \
	${BASE}/contours-all.osm.pbf
rm -f ${BASE}/contours-all.osm.pbf

# The cyclosm style selects geometry and height from a contours relation, where
# osm2pgsql has given us way and ele on planet_osm_line
psql -d ${DB} -qc "CREATE VIEW contours AS \
	SELECT way AS geometry, ele AS height FROM planet_osm_line WHERE ele IS NOT NULL"

# Left for renderDemZones.sh, which runs after renderd has been restarted
mv ${CHANGED_ZONES}.new ${CHANGED_ZONES}

echo "=========== done ==========="
