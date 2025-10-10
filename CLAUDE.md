# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

OGF-terrain-tools is a dual-purpose repository:

1. **Terrain Processing Tools**: Perl-based toolkit for converting OSM contour data into elevation tiles, SRTM-format height maps, and 3D terrain data for visualization
2. **OGF Infrastructure Scripts**: Operational scripts for running and maintaining the OpenGeofiction platform, including database backups, tile rendering, Overpass API updates, and monitoring

## Installation and Setup

```bash
# Install the Perl module
perl Makefile.PL
make
make install

# Configuration for terrain tools
# Copy ogftools.sample.conf to one of:
#   - Current directory
#   - $HOME/.ogf/ogftools.conf
#   - /etc/ogftools.conf
# Then edit to set:
#   - layer_path_prefix: Directory for output elevation tiles
#   - terrain_color_map: Path to DEM_poster.cpt color palette file
```

## Common Commands

### Terrain Processing (Full Pipeline)

```bash
# Convert OSM contour files to contour tiles at zoom level 13
perl bin/ogfElevation.pl 13 contours_01.osm contours_02.osm

# Or run the complete pipeline with -c option (all steps automatically)
perl bin/ogfElevation.pl -c 13 contours_01.osm contours_02.osm
```

### Manual Step-by-Step Terrain Processing

```bash
# Step 1: Convert contour tiles to elevation tiles
perl bin/makeElevationFromContour.pl contour:OGF:13:5724-5768:5984-6030

# Step 2: Reproject to SRTM 1x1 degree tiles
perl bin/makeSrtmElevationTile.pl OGF:13 1200 bbox=83,-59,85.001,-57.999

# Step 3: Create 3D elevation data for Web Worldwind
perl bin/make3dElevation.pl level=9 size=256 bbox=83,-59,85.001,-57.999

# Step 4: Create lower zoom levels and pack into ZIP
perl bin/convertMapLevel.pl -sz 256,256 -zip elev:WebWW:9:352-364:2992-3015 0
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

bin/monthlyActivitySummary.pl
  # Monthly aggregated activity reports

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

bin/simplifiedAdminPolygons.pl
  # Creates simplified versions of admin boundaries

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
bin/apacheLogInvestigate.pl
bin/debugAccessLog.pl
bin/debugDevelopmentLog.pl
  # Log analysis and debugging tools

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

# Development and Testing
bin/viewElevationTile.pl
  # Visualize elevation tile data

bin/editContourTiles.pl
  # Interactive contour tile editor

bin/checkUser.pl
bin/checkActiveStorageBlobs.pl
  # User and storage validation

bin/validateDbOverpassWays.pl
bin/relationIds.pl
  # Database validation utilities

bin/scpFileIfNotUpdating.pl
  # Conditional file transfer utility
```

### Compile TileUtil C Extension

```bash
# For performance-critical tile operations
cd TileUtil
perl Makefile.PL
make
```

## Architecture

### Module Organization

- **OGF::Data::** OSM data model (Node, Way, Relation, Context, Changeset)
  - `Context.pm`: Core data container, loads/saves OSM XML/OGF/PBF formats
  - `Node.pm`, `Way.pm`, `Relation.pm`: OSM object representations
  - `XML.pm`: SAX-based XML parser for OSM data
  - `Consolidate.pm`: Data consolidation utilities

- **OGF::Terrain::** Elevation processing pipeline
  - `ElevationTile.pm`: Core tile operations (makeArrayFromTile, makeTileFromArray, makeElevationFile)
  - `ContourLines.pm`: Contour extraction from OSM data
  - `ContourEditor.pm`: Interactive contour editing with Tk GUI
  - `Transform.pm`: Coordinate transformations
  - `PhysicalMap.pm`: Physical map generation
  - `RiverProfile.pm`: River elevation profiles

- **OGF::Geo::** Geometric operations
  - `Geometry.pm`: Point/line/polygon geometry
  - `Topology.pm`: Topological analysis and boundary processing
  - `Measure.pm`: Distance and area calculations

- **OGF::Util::** Utilities and helpers
  - `TileLevel.pm`: Tile zoom level management
  - `GlobalTile.pm`: Global tile coordinate systems
  - `Canvas.pm`: Drawing operations
  - `Shape.pm`: Geometric shape utilities
  - `StreamShape.pm`: Stream/river shape processing
  - `Overpass.pm`: Overpass API integration and query execution
  - `File.pm`: File I/O utilities
  - `Usage.pm`: Command-line usage helpers
  - `Line.pm`, `ElevationLine.pm`: Line processing utilities
  - `PPM.pm`: PPM image format handling

- **OGF::View::** Rendering and projection
  - `Projection.pm`: Coordinate system projections
  - `TileLayer.pm`: Tile layer management

- **OGF::Const, OGF::LayerInfo**: Constants and layer configuration

- **TileUtil/**: C extension module for performance-critical tile operations (surroundTile, convertTile, extractSubtile)

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
- `ogfutil-purgeWikiPagesSchedule[1-4].timer`: Wiki cleanup (multiple schedules)
- `overpass-daily-activity.service` + `.timer`: Daily activity summaries

**Configuration:**
- `etc/postgresql/`: PostgreSQL tuning configurations for OSM database and tile rendering
- `etc/apache2/`: Apache virtual host configurations

### Key Constants and Configuration

- Standard tile size: 512x512 pixels (`$T_WIDTH`, `$T_HEIGHT`)
- Bytes per pixel: 2 (shorts) or 4 (floats) (`$BPP`)
- No elevation value sentinel: -30001 (`$NO_ELEV_VALUE`)
- Tile coordinate systems follow OSM tiling scheme
- Most operational scripts use hardcoded paths to `/opt/opengeofiction/`

### Tile Range Descriptors

Scripts accept tile ranges in two formats:

1. Explicit tile ranges: `contour:OGF:13:5724-5768:5984-6030`
   - Format: `<type>:<layer>:<zoom>:<y-range>:<x-range>`

2. Bounding box: `contour:OGF:13:bbox=121,-21.85,122,-21.8`
   - Format: `<type>:<layer>:<zoom>:bbox=<minLon>,<minLat>,<maxLon>,<maxLat>`

### Elevation Processing Pipeline

1. **Contour to Tiles** (`ogfElevation.pl`): Parse OSM contour files into raster contour tiles at specified zoom level

2. **Interpolation** (`makeElevationFromContour.pl`): Convert contour tiles to elevation data using radius-based and weighted interpolation algorithms (implemented in C via TileUtil)

3. **Reprojection** (`makeSrtmElevationTile.pl`): Transform Mercator tiles to SRTM geographic projection (1x1 degree tiles)

4. **3D Generation** (`make3dElevation.pl`): Create elevation data for Web Worldwind 3D display

5. **Pyramid Building** (`convertMapLevel.pl`): Generate multi-resolution tile pyramids

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

- `Context` loads OSM/OGF/PBF → `ContourLines` extracts contours → `ElevationTile` creates raster tiles
- `ElevationTile` uses `TileUtil` (C extension) for heavy computation
- Stream data merged into contour layers via `StreamShape` utilities
- Projection handled by `View::Projection` and `Geo::LibProj::FFI`
- Operational scripts query via `OGF::Util::Overpass` wrapper

### File Formats

- `.osm`: OSM XML format
- `.ogf`: Custom OGF format (text-based, faster parsing)
- `.pbf`: Protocol Buffer format (converted to OSM via osmosis)
- `.cnr`: Contour tile files (binary)
- `.bil`: Binary elevation tiles
- `.hgt`: SRTM format height files
- `.dmp`: PostgreSQL dump files (custom format)
- `.osc`: OSM change files for replication

## Dependencies

Key Perl modules required (from Makefile.PL):
- Geo::LibProj::FFI (projections)
- XML::SAX (XML parsing)
- Archive::Zip
- Math::Trig
- Tk (for GUI editors)
- LWP, URI::Escape, HTML::Entities (web operations)
- Date::Format, Date::Parse, Time::HiRes
- JSON::PP, JSON::XS (JSON handling in operational scripts)

External tools used by operational scripts:
- `pg_dump`: PostgreSQL backup utility
- `planet-dump-ng`: Converts PostgreSQL OSM database to PBF format
- `pyosmium-get-changes`: Fetches OSM replication diffs
- `osmium`: OSM data manipulation tool
- `overpass/`: Overpass API server binaries
- `ncal`: Calendar calculations for backup scheduling

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
