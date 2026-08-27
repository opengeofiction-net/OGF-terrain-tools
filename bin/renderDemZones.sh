#!/bin/bash
#
# Expire the tiles over each zone which fetchDemData.sh changed. Run as the ogf
# user, by tile-refresh-dem@<style>.service, after renderd has restarted -
# renderd holds the VRT open, so acting before the restart would draw the old
# raster.
#
# /opt/opengeofiction/OGF-terrain-tools/bin/renderDemZones.sh <style>
#
# One sweep of the tile cache, every zoom, one call. demExpireTiles.py finds the
# metatiles which exist over a changed zone and render_expired marks them dirty:
# mod_tile keeps serving what it has and renders the replacement behind the
# reader, which is what it is for and what a change to a background hillshade
# wants.
#
# This replaced a forced render of z6-12 followed by expiry above. Forcing drew
# 4,425 metatiles across 33 zones, an hour and a quarter, when only 3,290 of
# them were cached at all - the rest were tiles nobody had asked for, rendered
# on the chance that somebody would. The sweep costs a readdir and a second.
#
# TOUCH_FROM is the one knob. At 0 every tile found is touched. Set it to 13 and
# the cached tiles below that are re-rendered there and then, correct before
# anyone looks rather than one request later, at the cost of the hour.
#
# Without any of this the tiles keep the old shading for up to three days: with
# no planet-import-complete file mod_tile makes a timestamp up, now minus three
# days, from getPlanetTime() in store_file.c, and treats a tile as expired only
# once it is older. Which leaves the recently rendered stale longest, and those
# are the ones people are looking at.
#
# changed-zones carries the footprint alongside each zone name, recorded by
# fetchDemData.sh. A zone which has been taken out of the render has no raster
# left to measure, and its tiles are precisely the ones which have to go.

set -e

if [ $# -ne 1 ]; then
	echo "Usage: $0 <style>" >&2
	exit 1
fi
STYLE=$1

BASE=/opt/opengeofiction/dem
TOOLS=${TOOLS:-/opt/opengeofiction/OGF-terrain-tools}
CHANGED_ZONES=${BASE}/changed-zones
TILE_DIR=${TILE_DIR:-$(sed -n 's/^tile_dir=//p' /etc/renderd.conf 2>/dev/null | head -1)}
TILE_DIR=${TILE_DIR:-/var/cache/renderd/tiles}
MIN_ZOOM=${MIN_ZOOM:-0}
TOUCH_FROM=${TOUCH_FROM:-0}
MAX_LOAD=${MAX_LOAD:-6}

# Run as whoever renderd writes tiles as. Marking a tile dirty sets its mtime
# back twenty years, and utime() with an explicit time needs ownership - group
# write does not do it. render_expired reports each refusal and exits 0 anyway,
# so the wrong user is a run that looks clean and expires nothing: it printed
# "Operation not permitted" 784 times before this check existed.
#
# The whole script runs as that user rather than only the expiry, because the
# whole script is a tile cache operation: it reads changed-zones and renderd.conf
# and walks the cache, all of which are world readable, and touches tiles, which
# are not. Nothing in it writes anywhere else
OWNER=$(stat -c %U "${TILE_DIR}/${STYLE}" 2>/dev/null || true)
if [ -n "${OWNER}" ] && [ "${OWNER}" != "$(id -un)" ] && [ "$(id -u)" != 0 ]; then
	echo "$0: tiles belong to ${OWNER} and this is $(id -un), which cannot" >&2
	echo "  set their timestamps. Run it as ${OWNER}:" >&2
	echo "    sudo runuser -u ${OWNER} -- $0 ${STYLE}" >&2
	exit 1
fi

# Nothing changed, so nothing to expire
[ -s ${CHANGED_ZONES} ] || exit 0

echo "=========== expiring z${MIN_ZOOM} and above ==========="
sed 's/^/  /' ${CHANGED_ZONES}

${TOOLS}/bin/demExpireTiles.py ${CHANGED_ZONES} ${STYLE} ${MIN_ZOOM} |
	render_expired --map=${STYLE} --min-zoom=${MIN_ZOOM} \
		--touch-from=${TOUCH_FROM} --max-load=${MAX_LOAD} --no-progress

# changed-zones is not removed here. It belongs to fetchDemData.sh, which writes
# it at the end of a run and removes it when nothing changed, so it is always
# either current or gone - and this reads it rather than owning it. Leaving it
# also makes a re-run repeat the same expiry, which is what you want from a step
# that only marks tiles dirty
echo "=========== done ==========="
