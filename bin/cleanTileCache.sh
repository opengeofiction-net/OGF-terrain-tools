#!/bin/bash
#
# Keeps the renderd tile cache from filling its filesystem.
#
# renderd never removes a tile, so the cache only grows: tiles05b reached 23 GB
# in the eighteen days after it was built. Nothing in mod_tile does this either
# - render_expired marks tiles dirty and render_old redraws them, neither
# deletes anything.
#
# Two passes, the second only if the first was not enough, and neither runs at
# all while the filesystem is below the threshold.
#
#   1. Tiles already marked dirty. expireTiles.sh runs render_expired
#      --touch-from, which winds a metatile's mtime back twenty years so
#      renderd knows to redraw it. Nothing else on the box has an mtime like
#      that, so it is an exact marker rather than a heuristic. A tile dirtied
#      twice lands forty years back, hence a ten year test rather than twenty.
#
#      These go whatever their traffic. The touch moves mtime and leaves atime
#      alone, so a good number of them are being served, wrong, right now.
#      Deleting one costs a render on the next request instead of a stale tile
#      and a background render - which is the trade this pass is making.
#
#   2. Tiles nothing has asked for, by atime rather than mtime. mtime is when a
#      tile was drawn: one drawn a month ago and served a thousand times a day
#      is not a candidate, and mtime cannot tell it from one drawn a month ago
#      and never touched since. Measured on tiles06b, 39% of the cache had not
#      been read in a day.
#
# Low zooms are never deleted. z0-10 is about 1 GB against a 23 GB cache and
# 185 of the 20,387 dirty tiles on tiles05b, so it frees nothing worth having,
# but it is the slowest to draw and the lowzoom and midzoom timers rebuild it
# twice a day regardless. Deleting it only blocks real requests.
#
# renderd is not restarted, and must not be. A deleted metatile is rendered
# again on the next request - there is no in-memory index to invalidate - and a
# restart would drop the render queue and every render in flight.
#

set -e

THRESHOLD=${THRESHOLD:-80}
# Ten years, not twenty: the touch is relative to the mtime it finds, so a tile
# dirtied twice is forty years back. Nothing legitimately rendered is a decade
# old - these servers are not
DIRTY_DAYS=${DIRTY_DAYS:-3650}
STALE_DAYS=${STALE_DAYS:-14}
# Zooms at or below this are never deleted
KEEP_ZOOM=${KEEP_ZOOM:-10}
RENDERD_CONF=${RENDERD_CONF:-/etc/renderd.conf}

TILE_DIR=${TILE_DIR:-$(sed -n 's/^tile_dir=//p' ${RENDERD_CONF} | head -1)}
[ -n "${TILE_DIR}" ] || { echo "no tile_dir in ${RENDERD_CONF}" >&2; exit 1; }
[ -d "${TILE_DIR}" ] || { echo "${TILE_DIR} is not a directory" >&2; exit 1; }

# One at a time. Two sweeps walking and deleting the same tree would each be
# reporting on the other's work
exec 9>"${TILE_DIR}/.clean.lock"
if ! flock -n 9; then
	echo "another run is in progress, giving up" >&2
	exit 1
fi

used() { df -P "${TILE_DIR}" | awk 'NR==2 { gsub(/%/,"",$5); print $5 }'; }

# The zoom directories deep enough to be fair game, across every style sharing
# this cache. The filesystem is per host, not per style
zoom_dirs() {
	local style zdir z
	for style in "${TILE_DIR}"/*/; do
		[ -d "${style}" ] || continue
		for zdir in "${style}"*/; do
			[ -d "${zdir}" ] || continue
			z=$(basename "${zdir}")
			case ${z} in ''|*[!0-9]*) continue ;; esac
			[ "${z}" -le "${KEEP_ZOOM}" ] && continue
			printf '%s\n' "${zdir}"
		done
	done
}

# $1 description, rest find test. Prints what it removed; the size has to be
# read before the unlink, so a failed delete would be counted as freed
sweep() {
	local what=$1; shift
	local dirs result
	mapfile -t dirs < <(zoom_dirs)
	[ ${#dirs[@]} -gt 0 ] || { echo "  ${what}: no zoom directories above z${KEEP_ZOOM}"; return 0; }
	result=$(find "${dirs[@]}" -name '*.meta' "$@" -printf '%s\n' -delete 2>/dev/null |
		awk '{ n++; s+=$1 }
		     END { u="B"; v=s+0
		           if (v >= 1073741824) { v/=1073741824; u="GB" }
		           else if (v >= 1048576) { v/=1048576; u="MB" }
		           else if (v >= 1024) { v/=1024; u="kB" }
		           printf "%d metatiles, %.1f %s\n", n+0, v, u }')
	echo "  ${what}: ${result}"
}

PCT=$(used)
echo "=========== ${TILE_DIR} at ${PCT}%, threshold ${THRESHOLD}% ==========="
if [ "${PCT}" -lt "${THRESHOLD}" ]; then
	echo "nothing to do"
	exit 0
fi

echo "=========== pass 1: dirty ==========="
sweep "dirty (mtime older than ${DIRTY_DAYS} days)" -mtime +${DIRTY_DAYS}

PCT=$(used)
echo "now at ${PCT}%"
if [ "${PCT}" -ge "${THRESHOLD}" ]; then
	# Under noatime every tile looks equally cold and this pass would take the
	# whole cache. Refuse rather than do that
	if findmnt -no OPTIONS --target "${TILE_DIR}" | tr ',' '\n' | grep -qx noatime; then
		echo "${TILE_DIR} is mounted noatime, so atime says nothing and pass 2 would delete everything above z${KEEP_ZOOM}. Remount relatime to enable it" >&2
	else
		echo "=========== pass 2: not served recently ==========="
		sweep "cold (atime older than ${STALE_DAYS} days)" -atime +${STALE_DAYS}
		PCT=$(used)
		echo "now at ${PCT}%"
	fi
fi

# find -delete leaves the tree behind it. Below the zoom level only, so a style
# or zoom directory is never removed from under renderd
find "${TILE_DIR}" -mindepth 3 -type d -empty -delete 2>/dev/null || true

if [ "${PCT}" -ge "${THRESHOLD}" ]; then
	echo "still at ${PCT}% after both passes - the cache is not what is filling ${TILE_DIR}, look at what else is on that filesystem" >&2
	exit 1
fi
echo "=========== done, ${PCT}% ==========="
