# OGF-terrain-tools

Scripts, systemd units and configuration for running [OpenGeofiction](https://opengeofiction.net).
Checked out on each server at `/opt/opengeofiction/OGF-terrain-tools` and run
from there, mostly by the systemd units in `etc/systemd/system`.

Not a Perl distribution, despite the history: there is nothing to install and no
`Makefile.PL`. Clone it, point the units at it, and make sure the Perl and
Python modules the scripts use are present.

The infrastructure itself is documented separately, in the admin wiki, built
from the [docs](https://git.opengeofiction.net/OpenGeofiction/docs-internal)
repository.

## What is here

| | |
| --- | --- |
| `bin/*.sh` | backups, Overpass, tile rendering and replication, site up and down |
| `bin/*.pl` | Overpass-driven jobs - coastline, territory polygons, continents, user lists, activity - and log analysis |
| `bin/dem*` | the elevation process: contour squares to DEM, hillshade, relief and contours |
| `lib/OGF/` | the Perl the above share: an OSM data model, Overpass, geometry |
| `etc/systemd/system/` | the units which run all of it |
| `etc/` | PostgreSQL tuning, Apache configuration, render style patches |

## Configuration

Some Perl scripts read `ogftools.conf`, from the working directory,
`$HOME/.ogf/ogftools.conf` or `/etc/ogftools.conf`. Copy
`ogftools.sample.conf` and edit it.

The Overpass-driven scripts also want `$HOME/.osmtoolsrc` for the API user,
password and url.

## The elevation process

Contours are drawn by hand, one `.osm` file per degree square, and are the source
of everything else. `buildDemData.sh` on the utility server builds the zones
whose squares have changed; `fetchDemData.sh` on a tile server fetches the
result and loads it.

    buildDemData.sh [zone ...]      # all changed zones, or the ones named
    buildDemZone.sh <zone>          # one zone, start to finish
    demMakeSquare.py <dir> N42E017  # a blank square to draw in
    demRecoverSquares.py …          # squares back out of a DEM, for a lost zone

See *Admin:Elevation process* in the wiki for what it produces and why, and
*Admin:Coastline process* for the sea level data it depends on.

## History

This began as a Perl distribution written by Thilo Stapff for turning
hand-drawn contours into elevation tiles, and grew the operational scripts
later. The terrain half was replaced by a GDAL pipeline in August 2026 and the
Perl behind it removed; it remains at the tag `thilo-dem-process`, and what it
could do is written up in *Admin:Elevation process*. Rather more than contour
conversion, as it turns out.

## Licence

Copyright &copy; 2017-2020 Thilo Stapff

Copyright &copy; 2020-2026 Lee Kindness and OpenGeofiction administrators

Free software, on the same terms as Perl itself: either Perl version 5.16.0 or,
at your option, any later version of Perl 5 you may have available.
