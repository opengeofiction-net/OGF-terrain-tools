# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Scripts, systemd units and configuration for running the OpenGeofiction
platform: backups, tile rendering and replication, Overpass, coastline and
elevation processing, territory polygons, monitoring.

Not a Perl distribution, despite the history. There is nothing to install: it is
checked out at `/opt/opengeofiction/OGF-terrain-tools` on each server and run
from there, mostly by the units in `etc/systemd/system`.

The infrastructure is documented in the admin wiki, built from the `docs`
repository - `Admin:Elevation process`, `Admin:Coastline process`,
`Admin:Creating a utility server` and so on. Those guides carry the reasoning;
this file is the map of the code.

## Configuration

Some Perl scripts read `ogftools.conf` from the working directory,
`$HOME/.ogf/ogftools.conf` or `/etc/ogftools.conf` - copy
`ogftools.sample.conf`. The Overpass-driven ones also want `$HOME/.osmtoolsrc`
for the API user, password and url.

## Common Commands

### Elevation

Contours are drawn by hand, one `.osm` per degree square under
`/opt/opengeofiction/elevation/osm-squares/<zone>/`, and are the source of
everything else. GDAL does the raster work; no Perl is involved.

```bash
# on the utility server: every zone whose squares have changed
bin/buildDemData.sh [zone ...]

# one zone, start to finish. KEEP_WORK=1 to keep the intermediates
bin/buildDemZone.sh <zone>

# a blank square for ground nobody has drawn
bin/demMakeSquare.py <outdir> N42E017 S03W121

# squares back out of a DEM, for a zone whose source was lost
bin/demRecoverSquares.py <dem.tif> <outdir> --water <water.osm.pbf>

# on a tile server: fetch what was published and load it
bin/fetchDemData.sh <style> [zone ...]
bin/renderDemZones.sh <style>
```

### Operational Scripts

```bash
# Database and Planet Backups
bin/backupPlanet.sh <backup_dir> <db_name> <publish_dir>
  # Creates pg_dump + planet-dump-ng OSM.PBF backups
  # Daily/weekly/monthly/yearly rotation based on day of week
  # Queues backups for S3 upload via backup-to-s3-queue

bin/backupPlanetSpinup.sh
  # Manages spinup process for planet backups

bin/backupWiki.sh
  # Backs up wiki content

bin/backupToS3.sh
  # Uploads queued backups to S3 storage

# Overpass API Management
bin/overpassUpdateDB.sh
  # Updates Overpass database from replication feed
  # Uses pyosmium-get-changes for incremental updates
  # Runs continuously via systemd (overpass-update.service)

bin/overpassImportDB.sh
  # Initial import of data into Overpass database

bin/overpassUpdateAreas.sh
  # Updates Overpass area data

bin/overpassCacheAirports.pl
bin/overpassCacheEconomy.pl
  # Pre-cache commonly requested Overpass queries

# Tile Rendering and Management
bin/tileReplicate.sh
  # Replicates tiles across servers

bin/expireTiles.sh
  # Expires outdated map tiles for re-rendering

# Coastline Processing
bin/coastlineProcess.pl
  # Validates and processes coastline data into shapefiles
  # Runs every 30 minutes via coastline-process.timer

bin/coastlineProcessDiff.sh
  # Processes coastline diff changes

# User Activity and Statistics
bin/dailyActivitySummary.pl
  # Generates daily activity reports
  # Creates geographic activity summaries

bin/userList.pl
  # Exports user listing data

bin/changesetInfo.pl
  # Extracts changeset information

# Administrative Boundaries and Map Data
bin/adminPolygonsToMultimap.pl
  # Exports admin boundaries to multimap format (GeoJSON)
  # Used for territory map display

bin/adminPolygonsToMultimapTimezone.pl
  # Exports timezone-aware admin polygons

bin/simplifiedAdminPolygons.py
  # Creates simplified versions of admin boundaries (Python rewrite of the Perl original, ~8x faster)

bin/checkContinent.pl
  # Validates continent boundary data

bin/geojsonToMultimap.pl
  # Converts GeoJSON to multimap format

# Server Monitoring and Maintenance
bin/sysStats.sh
bin/sysStatsPassenger.sh
  # System statistics collection

bin/parseSysStats.pl
  # Parses and analyzes system statistics

bin/parseAccessLog.pl
  # Parses Apache access logs into a database. On cron on the API server,
  # with checkUser.pl run against its output
bin/debugDevelopmentLog.pl
  # A simple view into the openstreetmap-website Rails logs

bin/kickApacheLog.sh
  # Rotates Apache logs

# Site Management
bin/ogf-set-online.sh
bin/ogf-set-read-only.sh
  # Controls site availability modes

bin/promote-diary-entry.sh
  # Promotes diary entries to featured status

bin/purgeWikiPages.pl
  # Cleans up wiki pages

# Database Replication
bin/osmdbtReplication.sh
  # Manages osmdb replication

# Infrastructure Provisioning
bin/createLinode.sh
  # Automates Linode server creation

# Ad hoc
bin/checkUser.pl
  # On the API server, against parseAccessLog.pl's output
bin/checkActiveStorageBlobs.pl
  # Storage validation. Niche
bin/syncDocsToWiki.py
  # Pulls the docs-internal repo (Admin: docs, .md canonical), rebuilds the
  # .wiki output with make, and syncs changed pages to the Admin: namespace
  # on the wiki (wikipage: front matter maps .md -> wiki page title). Runs
  # daily via cron; logs a docsSync entry to var/daily-book every run.
```

## Architecture

### Module Organization

Fourteen modules, all of them reached from scripts which run. The terrain half of
this library - `OGF::Terrain`, `LayerInfo`, `View::TileLayer`, the tile
utilities, and `Data::Consolidate` - was removed in August 2026 with the process
it served, and is available at the tag `thilo-dem-process`.

- **OGF::Data::** the OSM data model
  - `Context.pm`: the data container, loads and saves OSM XML, OGF and PBF
  - `Node.pm`, `Way.pm`, `Relation.pm`: OSM objects. `Relation` requires
    `Geo::Topology` for way component assembly
  - `Changeset.pm`, `XML.pm`: changesets, and the SAX parser

- **OGF::Geo::**
  - `Geometry.pm`: point, line and polygon geometry
  - `Topology.pm`: way sequences and boundary assembly

- **OGF::Util::**
  - `Overpass.pm`: Overpass queries, which most of the Perl here depends on
  - `File.pm`, `Usage.pm`: file I/O and command line usage

- **OGF::View::Projection**, **OGF::Const**: projections and constants

### Systemd Services and Timers

Located in `etc/systemd/system/`:

**Core Services:**
- `planet-backup.service`: Daily/weekly/monthly planet file backups
- `backup-to-s3.service`: S3 backup uploads
- `overpass-update.service`: Continuous Overpass DB updates
- `overpass-dispatcher.service`, `overpass-area-dispatcher.service`, `overpass-area-processor.service`: Overpass query processing
- `coastline-process.service` + `.timer`: Coastline validation every 30 minutes

**Tile Rendering:**
- `tile-replicate@.service`: Template for tile replication
- `tile-render-lowzoom@.timer` + `tile-render-midzoom@.timer` + `.service`: Scheduled tile rendering
- `tile-refresh-external-data@.service` + `.timer`: External data refresh

**Scheduled Utilities:**
- `ogfutil-checkContinent.timer` + `.service`: Continent boundary validation
- `ogfutil-backupWiki.timer`: Wiki backups
- `ogfutil-simplifiedAdminPolygons.timer` + `.service`: Admin boundary simplification
- `ogfutil-adminPolygonsToMultimap.timer` + `.service`: Export admin boundaries
- `ogfutil-userList.timer` + `.service`: User list exports
- `dem-build.timer` + `.service`: elevation zones, weekly, rebuilding only
  those whose contour squares have changed
- `ogfutil-purgeWikiPagesSchedule[1-4].timer`: Wiki cleanup (multiple schedules)
- `overpass-daily-activity.service` + `.timer`: Daily activity summaries

**Configuration:**
- `etc/postgresql/`: PostgreSQL tuning configurations for OSM database and tile rendering
- `etc/apache2/`: Apache virtual host configurations

### Key Constants and Configuration

- Most scripts hardcode paths under `/opt/opengeofiction/`. The elevation ones
  take `OGF`, `BASE`, `PUB` and `TOOLS` from the environment so they can be run
  against a copy off the server
- The tile constants which were here - `$T_WIDTH`, `$BPP`, `$NO_ELEV_VALUE` -
  went with the terrain modules

### Tile Range Descriptors

Scripts accept tile ranges in two formats:

1. Explicit tile ranges: `contour:OGF:13:5724-5768:5984-6030`
   - Format: `<type>:<layer>:<zoom>:<y-range>:<x-range>`

2. Bounding box: `contour:OGF:13:bbox=121,-21.85,122,-21.8`
   - Format: `<type>:<layer>:<zoom>:bbox=<minLon>,<minLat>,<maxLon>,<maxLat>`

### Elevation Processing

Per zone, `buildDemZone.sh`, all of it GDAL:

1. `demZoneExtent.py` decides which squares hold contours, and the grids - 1
   arcsecond for the master, 3 for the archive and the compatibility zip. Both
   grid registered, SRTM style, corner half a pixel outside the degree
2. `ogr2ogr` collects every way with a numeric `ele` - contours and the water
   edges at zero alike - through `etc/dem_osmconf.ini`, which exists because
   GDAL's default ignores `ele`
3. `gdal_rasterize`, then `gdal_fillnodata` bounded to 1.85 km
4. `demLandClamp.py` separates land at sea level from the sea itself
5. a box filter through a VRT kernel, for hillshading only
6. `gdalwarp` to mercator, `gdaldem` for hillshade and relief
7. `gdal_contour` and `demContoursToOsm.py` for the contour vectors
8. `.hgt` slices, the compatibility zip, publish, then `demZoneStats.py`

`dem/active-zones.txt` says which zones the renderers should load, which is a
different question from which are published - see `elevation/inactive`.

### Operational Data Flow

**Planet Backup Flow:**
1. `backupPlanet.sh` creates PostgreSQL dump via `pg_dump`
2. `planet-dump-ng` converts dump to OSM.PBF format
3. Files staged in backup queue directory
4. `backupToS3.sh` uploads to S3 storage
5. Published to public download directory

**Overpass Update Flow:**
1. `pyosmium-get-changes` fetches minutely diffs from replication server
2. `overpass/update_from_dir` applies diffs to Overpass database
3. Area processor updates derived area data
4. Dispatcher handles incoming queries

**Tile Rendering Flow:**
1. Database changes expire relevant tiles via `expireTiles.sh`
2. Systemd timers trigger rendering jobs at different zoom levels
3. `tileReplicate.sh` distributes tiles to CDN/mirror servers
4. External data refreshed periodically (coastlines, admin boundaries)

**Activity Tracking:**
1. `dailyActivitySummary.pl` queries Overpass for daily edits
2. Generates geographic summary data by region
3. `monthlyActivitySummary.pl` aggregates for longer periods
4. Results stored for statistics display

### Data Flow Between Modules

- `OGF::Util::Overpass` is the common dependency: most Perl here is an Overpass
  query, some processing, and a file written for the wiki or the website
- `Context` loads OSM XML, OGF or PBF; `Relation` reaches `Geo::Topology` to
  assemble way components, which is what territory and coastline work needs
- Projection through `View::Projection` and `Geo::LibProj::FFI`
- The elevation pipeline shares nothing with the Perl: GDAL, and Python where
  GDAL needs driving

### File Formats

- `.osm`: OSM XML format
- `.ogf`: Custom OGF format (text-based, faster parsing)
- `.pbf`: Protocol Buffer format (converted to OSM via osmosis)
- `.hgt`: SRTM format height files, 1201 samples square
- `.tif`: the DEM, hillshade and relief rasters, tiled and DEFLATE
- `.osm.xz`: the published contour squares, which JOSM opens directly
- `.dmp`: PostgreSQL dump files (custom format)
- `.osc`: OSM change files for replication

## Dependencies

Perl: Geo::LibProj::FFI, XML::SAX, Math::Trig, LWP, URI::Escape,
HTML::Entities, Date::Format, Date::Parse, Time::HiRes, JSON::XS, DBI for the
log analysis. Tk is no longer needed, the GUI editors having gone.

Python: `python3-gdal`, `python3-numpy`, `python3-pyosmium`, `python3-lxml`.

External tools used by operational scripts:
- `pg_dump`: PostgreSQL backup utility
- `planet-dump-ng`: Converts PostgreSQL OSM database to PBF format
- `pyosmium-get-changes`: Fetches OSM replication diffs
- `osmium`: OSM data manipulation tool
- `overpass/`: Overpass API server binaries
- `ncal`: Calendar calculations for backup scheduling
- `gdal-bin`, `osmium`, `xz`, `zip`: the elevation process

## Environment Assumptions

Most operational scripts assume deployment under `/opt/opengeofiction/`:
- `/opt/opengeofiction/OGF-terrain-tools/`: This repository
- `/opt/opengeofiction/backup/`: Backup staging directory
- `/opt/opengeofiction/backup-to-s3-queue/`: S3 upload queue
- `/opt/opengeofiction/overpass/`: Overpass API data and config
- `/opt/opengeofiction/planet-dump-ng/`: Planet dump tool
- `/var/www/html/data.opengeofiction.net/public_html/backups/`: Public backup directory

Database names referenced:
- `ogfdevapi`: Primary OGF database (used in production)
