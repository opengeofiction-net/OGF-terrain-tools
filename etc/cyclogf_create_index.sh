#!/bin/sh

DB=cyclogf

# from https://github.com/cquest/osmfr-cartocss/blob/9a7543eaee34fbb551e45550f3a14a7473639b17/create_index.sh
psql $DB -c "create index planet_osm_polygon_place on planet_osm_polygon using spgist(way) where place is not null;" &
psql $DB -c "create index planet_osm_polygon_refinsee on planet_osm_polygon using spgist(way) where tags ? 'ref:INSEE';" &
psql $DB -c "create index planet_osm_polygon_adminboundary on planet_osm_polygon using spgist(way) where boundary is not null OR admin_level is not null;" &
psql $DB -c "create index planet_osm_polygon_parks on planet_osm_polygon using spgist(way) where boundary='national_park' OR tourism = 'theme_park';" &
psql $DB -c "create index planet_osm_polygon_nobuilding on planet_osm_polygon using spgist(way) where building is null;" &
psql $DB -c "create index planet_osm_polygon_water on planet_osm_polygon using spgist(way) where landuse IS NOT NULL OR waterway IS NOT NULL OR \"natural\" IS NOT NULL OR amenity = 'fountain'::text;" &
psql $DB -c "create index planet_osm_polygon_named on planet_osm_polygon using spgist(way) WHERE name is not null or COALESCE(name, tags -> 'name:fr'::text, tags -> 'int_name'::text, tags -> 'stars'::text, tags -> 'ele'::text, tags -> 'ele:local'::text, ref, tags -> 'school:FR'::text) IS NOT NULL;" &
psql $DB -c "create index planet_osm_polygon_placenames_sea on planet_osm_polygon using spgist(way) WHERE coalesce(\"natural\",place) = ANY (ARRAY['archipelago','island','ocean','sea','bay','strait','isthmus']);" &
psql $DB -c "create index planet_osm_polygon_landcover on planet_osm_polygon using spgist(way) WHERE COALESCE(landuse, wetland, leisure, aeroway, amenity, military, power, \"natural\", tourism, highway, man_made) IS NOT NULL OR (building = ANY (ARRAY['civic'::text, 'public'::text]));" &
psql $DB -c "create index planet_osm_polygon_xlarge on planet_osm_polygon using spgist(way) WHERE way_area > 25000::double precision;" &
psql $DB -c "create index planet_osm_polygon_xxlarge on planet_osm_polygon using spgist(way) WHERE way_area > 100000::double precision;" &
psql $DB -c "create index planet_osm_polygon_xxxlarge on planet_osm_polygon using spgist(way) WHERE way_area > 400000::double precision;" &
psql $DB -c "create index planet_osm_polygon_poi ON planet_osm_polygon USING spgist(ST_PointOnSurface(way)) WHERE COALESCE(amenity, aeroway, military, barrier, man_made, railway, \"natural\", power, shop, tourism, waterway, historic, leisure, highway) IS NOT NULL OR tags ? 'mountain_pass'::text OR tags ? 'emergency'::text OR tags ? 'craft'::text OR tags ? 'diplomatic'::text OR tags ? 'healthcare' or office is not null" &

psql $DB -c "create index planet_osm_point_capital on planet_osm_point using spgist(way) where place IS NOT NULL AND (capital IS NOT NULL OR tags ? 'is_capital'::text);" &
psql $DB -c "create index planet_osm_point_place on planet_osm_point using spgist(way) where place IS NOT NULL;" &
psql $DB -c "create index planet_osm_point_refinsee on planet_osm_point using spgist(way) where tags ? 'ref:INSEE';" &
psql $DB -c "create index planet_osm_point_named on planet_osm_point using spgist(way) WHERE name is not null OR COALESCE(name, tags -> 'name:fr'::text, tags -> 'int_name'::text, tags -> 'stars'::text, ele, tags -> 'ele:local'::text, ref, tags -> 'school:FR'::text) IS NOT NULL;" &
psql $DB -c "create index planet_osm_point_placenames on planet_osm_point using spgist(way) WHERE place = ANY (ARRAY['city'::text, 'town'::text]);" &
psql $DB -c "create index planet_osm_point_placenames_large on planet_osm_point using spgist(way) WHERE place = ANY (ARRAY['country'::text, 'state'::text, 'continent'::text]);" &
psql $DB -c "create index planet_osm_point_placenames_sea on planet_osm_point using spgist(way) WHERE coalesce(\"natural\",place) = ANY (ARRAY['archipelago','island','ocean','sea','bay','strait','isthmus']);" &
psql $DB -c "create index planet_osm_point_poi ON planet_osm_point USING spgist(way) WHERE COALESCE(amenity, aeroway, military, barrier, man_made, railway, \"natural\", power, shop, tourism, waterway, historic, leisure, highway) IS NOT NULL OR tags ? 'mountain_pass'::text OR tags ? 'emergency'::text OR tags ? 'craft'::text OR tags ? 'diplomatic'::text OR tags ? 'healthcare' or office is not null" &

psql $DB -c "create index planet_osm_line_refsandre on planet_osm_line using spgist(way) where tags ? 'ref:sandre';" &
psql $DB -c "create index planet_osm_line_highwayref on planet_osm_line using spgist(way) where highway IS NOT NULL AND ref IS NOT NULL;" &
psql $DB -c "create index planet_osm_line_minor on planet_osm_line using spgist(way) where highway IS NOT NULL OR railway IS NOT NULL OR aeroway IS NOT NULL;" &
psql $DB -c "create index planet_osm_line_ref on planet_osm_line using spgist(way) where ref IS NOT NULL;" &
psql $DB -c "create index planet_osm_line_manmade on planet_osm_line using spgist(way) WHERE man_made IS NOT NULL;" &
psql $DB -c "create index planet_osm_line_named on planet_osm_line using spgist(way) WHERE name IS NOT NULL;" &
psql $DB -c "create index planet_osm_line_waterway on planet_osm_line using spgist(way) WHERE waterway IS NOT NULL;" &
psql $DB -c "create index planet_osm_line_ferry on planet_osm_line using spgist(way) where route='ferry'" &

#psql $DB -c "create index planet_osm_roads_highway on planet_osm_roads using spgist(way) where highway IS NOT NULL;" &
psql $DB -c "create index planet_osm_roads_adminboundary on planet_osm_roads using spgist(way) where boundary='administrative';" &
psql $DB -c "create index planet_osm_roads_main on planet_osm_roads using spgist(way) where highway IS NOT NULL or railway is not null;" &

# from https://github.com/cyclosm/cyclosm-cartocss-style/blob/master/docs/INSTALL.md#database-indexes
psql $DB -c "create index planet_osm_bicycle_routes on planet_osm_line using gist(way) where route='bicycle' OR route='mtb';" &
