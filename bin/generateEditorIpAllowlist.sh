#!/usr/bin/bash
# Regenerate the Apache RewriteMap of known OGF editor IPs from changeset data.
#
# Reads all changesets-*.txt under /opt/opengeofiction/ip-data (pipe-delimited:
# field 6 = registration IP, field 11 = changeset upload IP), dedupes, and
# writes an Apache txt RewriteMap (/etc/apache2/editor-ips.txt) with one
# "ip 1" entry per line. Reloads Apache (graceful) only when the map changed.
#
# Used by the "Element page views without referer" bot rule so that known
# editors who paste/type element URLs directly (no referer) are never blocked.

IPDATA=/opt/opengeofiction/ip-data
MAPFILE=/etc/apache2/editor-ips.txt
TMPFILE=$(mktemp /tmp/editor-ips.XXXXXX)

# Field 6 = registration IP, field 11 = changeset IP
# Exclude private/RFC1918/internal addresses — never external editor IPs
awk -F'|' '
  NF >= 12 {
    if ($6 != "-" && $6 != "" && $6 !~ /^(127\.|10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|0\.|255\.|::1$|::ffff:)/) print $6
    if ($11 != "-" && $11 != "" && $11 !~ /^(127\.|10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|0\.|255\.|::1$|::ffff:)/) print $11
  }
' "$IPDATA"/changesets-*.txt 2>/dev/null | sort -u | sed 's/$/ 1/' > "$TMPFILE"

ENTRIES=$(wc -l < "$TMPFILE")
echo "editor-ips: $ENTRIES known editor IPs"

if [ ! -f "$MAPFILE" ] || ! cmp -s "$TMPFILE" "$MAPFILE"; then
  sudo mv "$TMPFILE" "$MAPFILE"
  sudo chmod 644 "$MAPFILE"
  echo "editor-ips: map updated, reloading apache"
  sudo apache2ctl graceful
else
  rm -f "$TMPFILE"
  echo "editor-ips: map unchanged"
fi
