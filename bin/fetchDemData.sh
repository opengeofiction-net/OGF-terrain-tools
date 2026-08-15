#!/bin/bash
#
# Fetch the OGF contour and hillshade data used by the DEM layers, and load it.
# Run as the ogf user, by tile-refresh-dem@<style>.timer, or by hand.
#
# The zones themselves are produced by a separate, manual process - see
# Admin:How to add a new contour zone to ogf-topo
#
# /opt/opengeofiction/OGF-terrain-tools/bin/fetchDemData.sh <style> [zone ...]
#
# With no zones named every published zone is fetched, the list being read from
# the web server directory listing. Naming zones does just those. Either way the
# downloads are only fetched when the published copy is newer, and the VRT and
# the database are only rebuilt when something has actually changed.
#
# The zones which changed are left in dem/changed-zones, for renderDemZones.sh
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

BASE=/opt/opengeofiction/dem
SRC=https://ogfsrtm.rent-a-planet.com
OSM2PGSQL_STYLE=/opt/opengeofiction/OGF-terrain-tools/etc/cyclogf_contours.style
RAMP=/opt/opengeofiction/map-styles/${STYLE}/dem/shade.ramp
CHANGED_ZONES=${BASE}/changed-zones
DB=contours

mkdir -p ${BASE}/zips ${BASE}/hillshade ${BASE}/shade ${BASE}/contours ${BASE}/sorted

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
# leave an empty zip behind when the server is unreachable
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

# The zone list, either from the arguments or from the contours listing. The
# combined zone is a stale 2023 artefact and is skipped
if [ $# -gt 0 ]; then
	ZONES="$@"
else
	LISTING=$(curl -sS --fail --retry 3 --retry-delay 5 ${SRC}/contours/) || {
		echo "could not read the zone listing, nothing to do" >&2
		exit 1
	}
	ZONES=$(echo "${LISTING}" \
		| grep -o 'contours-[a-z0-9]*\.osm\.pbf' \
		| sed 's/contours-//; s/\.osm\.pbf//' \
		| grep -vx combined \
		| sort -u)
	[ -n "${ZONES}" ] || { echo "the zone listing was empty, nothing to do" >&2; exit 1; }
fi
echo "zones: $(echo ${ZONES} | tr '\n' ' ')"

CHANGED=""
FAILED=""

for ZONE in ${ZONES}; do
	echo "=========== zone-${ZONE} ==========="
	ZONE_CHANGED=""

	ZIP=${BASE}/zips/tiff-${ZONE}.zip
	rc=0; fetch ${SRC}/tiff-files/tiff-${ZONE}.zip ${ZIP} || rc=$?
	[ ${rc} -eq 0 ] && ZONE_CHANGED=yes
	[ ${rc} -eq 2 ] && { FAILED="${FAILED} ${ZONE}"; continue; }

	PBF=${BASE}/contours/contours-${ZONE}.osm.pbf
	rc=0; fetch ${SRC}/contours/contours-${ZONE}.osm.pbf ${PBF} || rc=$?
	[ ${rc} -eq 0 ] && ZONE_CHANGED=yes
	[ ${rc} -eq 2 ] && { FAILED="${FAILED} ${ZONE}"; continue; }

	# Only the 90m hillshade is used, the rest of the zip being the working
	# files of the generation process, and the other resolutions which the VRT
	# overviews replace. -p as every zip holds the same file name, under the
	# old geofictician paths
	TIF=${BASE}/hillshade/${ZONE}.tif
	if [ ! -f ${TIF} ] || [ ${ZIP} -nt ${TIF} ]; then
		echo "extracting hillshade-90.tif"
		unzip -p ${ZIP} '*/hillshade-90.tif' > ${TIF}
		ZONE_CHANGED=yes
	fi

	# The greyscale hillshade cannot be used directly. gdaldem gives flat ground
	# a valid mid grey, around 180, not nodata - for gobras that is 98% of the
	# raster - so compositing it would darken everything inside the zone's
	# rectangle, sea included, with a hard edge at the boundary. The style's
	# ramp turns it into RGBA, flat ground fully transparent and only slopes
	# shaded, which is also a good deal smaller.
	#
	# Tiled, not the gdal default of one scanline per block: rendering reads
	# small windows at random, and a striped file has to inflate a full 5000
	# pixel wide strip for each
	SHADE=${BASE}/shade/${ZONE}.tif
	if [ ! -f ${SHADE} ] || [ ${TIF} -nt ${SHADE} ] || [ ${RAMP} -nt ${SHADE} ]; then
		echo "applying the shade ramp"
		gdaldem color-relief -alpha -q ${TIF} ${RAMP} ${SHADE} \
			-co TILED=YES -co BLOCKXSIZE=256 -co BLOCKYSIZE=256 \
			-co COMPRESS=DEFLATE -co PREDICTOR=2
		ZONE_CHANGED=yes
	fi

	# The generator writes node and way blocks interleaved, so each file needs
	# sorting before osm2pgsql, or the merge below, will take it. Sorting per
	# zone keeps the memory to the largest single zone, rather than the lot,
	# and the result is kept so it is only redone when the download changes
	SORTED=${BASE}/sorted/${ZONE}.osm.pbf
	if [ ! -f ${SORTED} ] || [ ${PBF} -nt ${SORTED} ]; then
		echo "sorting contours"
		osmium sort ${PBF} --overwrite -o ${SORTED}
		ZONE_CHANGED=yes
	fi

	[ -n "${ZONE_CHANGED}" ] && CHANGED="${CHANGED} ${ZONE}"
done

[ -n "${FAILED}" ] && echo "WARNING: zones which could not be fetched:${FAILED}" >&2

# Normally there is nothing new, and the rest is not worth doing. The existing
# data is left exactly as it is
if [ -z "${CHANGED}" ]; then
	echo "=========== no zones changed ==========="
	rm -f ${CHANGED_ZONES}
	exit 0
fi
echo "=========== changed:${CHANGED} ==========="

# ----------- hillshade -----------------
# One VRT over every zone held locally, with overviews so the low zooms do not
# have to read the 90m rasters.
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
# Loaded from scratch, there being no incremental update to do. No --slim for
# the same reason, which keeps the database a good deal smaller
echo "=========== loading contours ==========="
psql -lqt | cut -d\| -f1 | grep -qw ${DB} || createdb -E UTF8 ${DB}
psql -d ${DB} -qc "CREATE EXTENSION IF NOT EXISTS postgis"
psql -d ${DB} -qc "DROP VIEW IF EXISTS contours"

# merge, not cat: osm2pgsql needs the input ordered, and cat concatenates, so
# the second zone's nodes would follow the first zone's ways. The merge streams
# the already sorted per-zone files, and each zone has its own id block, so
# there is nothing to deduplicate
osmium merge ${BASE}/sorted/*.osm.pbf --overwrite -o ${BASE}/contours-all.osm.pbf
osm2pgsql --database ${DB} --create --style ${OSM2PGSQL_STYLE} \
	--number-processes=4 \
	${BASE}/contours-all.osm.pbf
rm -f ${BASE}/contours-all.osm.pbf

# The cyclosm style selects geometry and height from a contours relation, where
# osm2pgsql has given us way and ele on planet_osm_line
psql -d ${DB} -qc "CREATE VIEW contours AS \
	SELECT way AS geometry, ele AS height FROM planet_osm_line WHERE ele IS NOT NULL"

# Left for renderDemZones.sh, which runs after renderd has been restarted
echo ${CHANGED} | tr ' ' '\n' | grep -v '^$' > ${CHANGED_ZONES}

echo "=========== done ==========="
