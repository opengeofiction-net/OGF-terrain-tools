#!/bin/bash
#
# Fetch the OGF contour and hillshade data used by the cyclogf DEM layers, and
# load it. Run as the ogf user, manually, when zones have been regenerated.
#
# The zones themselves are produced by a separate, manual process - see
# Admin:How to add a new contour zone to ogf-topo
#
# /opt/opengeofiction/OGF-terrain-tools/bin/fetchDemData.sh [zone ...]
#
# With no arguments every published zone is fetched, the zone list being read
# from the web server directory listing. Naming a zone, or zones, does just
# those, but still rebuilds the VRT and reloads the database from everything
# held locally.

set -e

BASE=/opt/opengeofiction/dem
SRC=https://ogfsrtm.rent-a-planet.com
STYLE=/opt/opengeofiction/OGF-terrain-tools/etc/cyclogf_contours.style
DB=contours

mkdir -p ${BASE}/zips ${BASE}/hillshade ${BASE}/contours

# The zone list, either from the arguments or from the contours listing. The
# combined zone is a stale 2023 artefact and is skipped
if [ $# -gt 0 ]; then
	ZONES="$@"
else
	ZONES=$(curl -sS ${SRC}/contours/ \
		| grep -o 'contours-[a-z0-9]*\.osm\.pbf' \
		| sed 's/contours-//; s/\.osm\.pbf//' \
		| grep -vx combined \
		| sort -u)
fi
echo "zones: $(echo ${ZONES} | tr '\n' ' ')"

for ZONE in ${ZONES}; do
	echo "=========== fetching zone-${ZONE} ==========="

	# -z only downloads when the remote copy is newer, --remote-time keeps the
	# timestamp so the comparison holds next run. The zips are kept for this
	ZIP=${BASE}/zips/tiff-${ZONE}.zip
	curl -sS --remote-time -o ${ZIP} $([ -f ${ZIP} ] && echo "-z ${ZIP}") \
		${SRC}/tiff-files/tiff-${ZONE}.zip

	PBF=${BASE}/contours/contours-${ZONE}.osm.pbf
	curl -sS --remote-time -o ${PBF} $([ -f ${PBF} ] && echo "-z ${PBF}") \
		${SRC}/contours/contours-${ZONE}.osm.pbf

	# Only the 90m hillshade is used, the rest of the zip being the working
	# files of the generation process, and the other resolutions which the VRT
	# overviews replace. -p as every zip holds the same file name, under the
	# old geofictician paths
	TIF=${BASE}/hillshade/${ZONE}.tif
	if [ ! -f ${TIF} ] || [ ${ZIP} -nt ${TIF} ]; then
		echo "extracting hillshade-90.tif"
		unzip -p ${ZIP} '*/hillshade-90.tif' > ${TIF}
	fi
done

# ----------- hillshade -----------------
# One VRT over every zone held locally, with overviews so the low zooms do not
# have to read the 90m rasters.
#
# COMPRESS_OVERVIEW is not optional. The mosaic spans the bounding box of every
# zone, which is most of the planet, and overviews are dense - the empty space
# between zones is written out in full. Two zones alone give an 88MB .ovr, and
# 476KB once the nodata compresses away
echo "=========== building shade.vrt ==========="
gdalbuildvrt -overwrite ${BASE}/shade.vrt ${BASE}/hillshade/*.tif
gdaladdo -r average --config COMPRESS_OVERVIEW DEFLATE \
	${BASE}/shade.vrt 2 4 8 16 32 64 128 256 512

# ----------- contours -----------------
# Loaded from scratch each time, there being no incremental update to do. No
# --slim for the same reason, which keeps the database a good deal smaller
echo "=========== loading contours ==========="
psql -lqt | cut -d\| -f1 | grep -qw ${DB} || createdb -E UTF8 ${DB}
psql -d ${DB} -qc "CREATE EXTENSION IF NOT EXISTS postgis"
psql -d ${DB} -qc "DROP VIEW IF EXISTS contours"

# merge, not cat: osm2pgsql needs the input ordered, and cat concatenates, so
# the second zone's nodes follow the first zone's ways. Each zone is generated
# into its own id block, so a streaming merge is safe and there is no need to
# sort the whole lot in memory
osmium merge ${BASE}/contours/contours-*.osm.pbf --overwrite -o ${BASE}/contours-all.osm.pbf
osm2pgsql --database ${DB} --create --style ${STYLE} \
	--number-processes=4 \
	${BASE}/contours-all.osm.pbf
rm -f ${BASE}/contours-all.osm.pbf

# The cyclosm style selects geometry and height from a contours relation, where
# osm2pgsql has given us way and ele on planet_osm_line
psql -d ${DB} -qc "CREATE VIEW contours AS \
	SELECT way AS geometry, ele AS height FROM planet_osm_line WHERE ele IS NOT NULL"

echo "=========== done ==========="
echo "restart renderd to pick up the new data:  sudo systemctl restart renderd"
