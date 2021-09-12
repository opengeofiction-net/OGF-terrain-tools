#! /usr/bin/perl -w -CSDA
# Parse errors from an osmcoastline db file to JSON for display in wiki
#
# sudo apt install libspatialite7
# apt install cpanminus
# apt install  libgeos-c1v5 libgeos++-dev apt install libproj-dev
# cpanm DBD::Spatialite
# apt install libsqlite3-mod-spatialite
# libdbd-sqlite3-perl
# get https://download.osgeo.org/proj/proj-4.9.3.tar.gz extract
# ./configure --prefix=/opt/opengeofiction/sw-proj4
# make
# sudo make install
# cpanm DBD::Spatialite




use lib '/opt/opengeofiction/OGF-terrain-tools';
use strict;
use warnings;
use File::Basename;
use DBI;
use Data::Dumper;
use POSIX 'strftime';
use OGF::Util::Usage qw( usageInit usageError );

my $DB_PREFIX = 'ogf-coastlines-unsplit';
my $DB_SUFFIX = 'db';
my $CHECK_POINTS = 1;
my $CHECK_LINES  = 0;

my %opt;
usageInit( \%opt, qq/ h dir=s dest=s cleanup=i/, << "*" );
[-dir <directory> -dest <filename> -cleanup]

-dir     source directory, will use latest db file there
-dest    destination file
-cleanup also remove db files older than n hours
*
my $SRC_DIR  = $opt{'dir'};
my $DST_FILE = $opt{'dest'};
my $CLEANUP  = (defined $opt{'cleanup'}) ? $opt{'cleanup'} : 0;
usageError() if( $opt{'h'} or !defined $SRC_DIR or !defined $DST_FILE );

my $startedutc = strftime '%Y-%m-%d %H:%M UTC', gmtime;

# find the latest, but not currently being copied, coastline db
my $dh;
unless( (-d $SRC_DIR) and (opendir $dh, $SRC_DIR) )
{
	print STDERR "$SRC_DIR does not exist, or not readable\n";
	exit 1;
}
my $newest = 0;
my $newest_file = undef;
while( my $file = readdir $dh )
{
	next unless( $file =~ /^$DB_PREFIX.+$DB_SUFFIX$/ );
	my (undef,undef,undef,undef,undef,undef,undef,undef,undef,$mtime) = stat "$SRC_DIR/$file";
	if( (time - $mtime > 30) and ($mtime > $newest) )
	{
		$newest = $mtime;
		$newest_file = $file;
	}
}
closedir $dh;
unless( defined $newest_file )
{
	print STDERR "$SRC_DIR does not contain valid $DB_PREFIX*.$DB_SUFFIX file\n";
	exit 1;
}
print "Using: $newest_file\n";

# cleanup old coastline db files
if( $CLEANUP > 0 )
{
	opendir $dh, $SRC_DIR;
	while( my $file = readdir $dh )
	{
		next unless( $file =~ /^$DB_PREFIX.+$DB_SUFFIX$/ );
		next if( $file eq $newest_file ); # don't remove rug!
		my (undef,undef,undef,undef,undef,undef,undef,undef,undef,$mtime) = stat "$SRC_DIR/$file";
		if( (time - $mtime) > (60 * $CLEANUP) )
		{
			print "Removing old $DB_SUFFIX: $file\n";
			system "rm $SRC_DIR/$file";
		}
	}
	closedir $dh;
}

# connect to database
$newest_file = "$SRC_DIR/$newest_file";
my $dbh = DBI->connect("dbi:Spatialite:dbname=$newest_file","","");
my $dbcreated = $dbh->selectrow_array('SELECT timestamp FROM meta', undef);

# add WGS84 to SRIDs
my $srid_exists = $dbh->selectrow_array('SELECT srid FROM spatial_ref_sys WHERE srid=4326', undef);
if( !defined $srid_exists )
{
	my $sth = $dbh->prepare("INSERT INTO spatial_ref_sys(srid,auth_name,auth_srid,ref_sys_name,proj4text,srtext) VALUES (?,?,?,?,?,?)");
	$sth->execute(4326, 'epsg', 4326, 'WGS 84', '+proj=longlat +datum=WGS84 +no_defs', 'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],AXIS["Latitude",NORTH],AXIS["Longitude",EAST],AUTHORITY["EPSG","4326"]]');
	$sth->finish();
}

# open temp file and output header
my $temp_dest = "$DST_FILE.tmp";
print "Writing to: $temp_dest\n";
my $OUTPUT;
unless( open $OUTPUT, '>', $temp_dest )
{
	$dbh->disconnect();
	print STDERR "Cannot open $temp_dest for writing\n";
	exit 1;
}
print $OUTPUT <<EOF;
[
EOF

# the business: check points
my($geom,$osm_id,$error,$n);
$n = 0;
my %nodes = ();
if( $CHECK_POINTS == 1 )
{
	print "Checking points...\n";
	
	# prepare SQL statement
	my $sth = $dbh->prepare("SELECT AsText(Transform(GEOMETRY,4326)) AS geom, osm_id, error FROM error_points")
		or die "prepare statement failed: $dbh->errstr()";
	$sth->execute() or die "execution failed: $dbh->errstr()"; 

	# loop through each row of the result set, and print it
	while( ($geom,$osm_id,$error) = $sth->fetchrow() )
	{
		my $sub = substr $geom, 0, 70;
		printf "P: %-70s / %d / %s\n", $sub, $osm_id, $error;
		
		if( $geom =~ /^POINT\(([\-\d]+\.[\d]+) ([\-\d]+\.[\d]+)\)$/ )
		{
			my $lat = $2; my $lon = $1;
			$nodes{"$lat:$lon"} = 1;
			print $OUTPUT "    },\n" unless( $n == 0 );
			
			if( $error eq 'tagged_node' )
			{
				print $OUTPUT <<EOF;
    {
        "text": "Node <a href=\\"http://opengeofiction.net/node/$osm_id\\">$osm_id</a> has natural=coastline property tags",
        "icon": "coastline/$error.png",
        "iconAnchor": [10, 10],
        "lon": $lon,
        "lat": $lat,
        "id": "$osm_id"
EOF
			}
			elsif( $error eq 'intersection' )
			{
				print $OUTPUT <<EOF;
    {
        "text": "Intersection of coastline ways",
        "icon": "coastline/$error.png",
        "iconAnchor": [10, 10],
        "lon": $lon,
        "lat": $lat,
        "ways": [
        ]
EOF
			}
			elsif( $error eq 'not_a_ring' )
			{
				print $OUTPUT <<EOF;
    {
        "text": "Not a ring: coastline could not be constructed into a closed polygon",
        "icon": "coastline/$error.png",
        "iconAnchor": [10, 10],
        "lon": $lon,
        "lat": $lat
EOF
			}
			elsif( $error eq 'unconnected' or $error eq 'fixed_end_point' )
			{
				my $icon = 'unconnected';
				print $OUTPUT <<EOF;
    {
        "text": "$error: Coastline is not closed",
        "icon": "coastline/$icon.png",
        "iconAnchor": [10, 10],
        "lon": $lon,
        "lat": $lat
EOF
			}
			elsif( $error eq 'double_node' )
			{
				print $OUTPUT <<EOF;
    {
        "text": "Node <a href=\\"http://opengeofiction.net/node/$osm_id\\">$osm_id</a> appears more than once in coastline",
        "icon": "coastline/$error.png",
        "iconAnchor": [10, 10],
        "lon": $lon,
        "lat": $lat,
        "id": "$osm_id"
EOF
			}
			else
			{
				print $OUTPUT <<EOF;
    {
        "text": "Error: $error",
        "icon": "red",
        "lon": $lon,
        "lat": $lat
EOF
				print STDERR "UNKNOWN: $geom,$osm_id,$error\n";
			}
			++$n;
		}
	}
	$sth->finish();
}

# and now check lines
if( $CHECK_LINES == 1 )
{
	# prepare SQL statement
	my $sth = $dbh->prepare("SELECT AsText(Transform(GEOMETRY,4326)) AS geom, osm_id, error FROM error_lines")
		or die "prepare statement failed: $dbh->errstr()";
	$sth->execute() or die "execution failed: $dbh->errstr()"; 

	# loop through each row of the result set, and print it
	while( ($geom,$osm_id,$error) = $sth->fetchrow() )
	{
		my $sub = substr $geom, 0, 70;
		printf "L: %-70s / %d / %s\n", $sub, $osm_id, $error;
		
		if( $geom =~ /^LINESTRING\(([\-\d]+\.[\d]+) ([\-\d]+\.[\d]+)/ )
		{
			my $lat1 = $2; my $lon1 = $1;
			next if( exists $nodes{"$lat1:$lon1"} ); # don't output if we already had a node report
			
			print $OUTPUT "    },\n" unless( $n == 0 );
			if( $error eq 'overlap' )
			{
				print $OUTPUT <<EOF;
    {
        "text": "Overlapping coastline, first node on way shown",
        "icon": "coastline/$error.png",
        "iconAnchor": [10, 10],
        "lon": $lon1,
        "lat": $lat1
EOF
			}
			elsif( $error eq 'direction' )
			{
				print $OUTPUT <<EOF;
    {
        "text": "Reversed coastline - should be counter-clockwise, first node on way shown",
        "icon": "coastline/wrong_direction.png",
        "iconAnchor": [10, 10],
        "lon": $lon1,
        "lat": $lat1
EOF
			}
			else
			{
				print $OUTPUT <<EOF;
    {
        "text": "Error lines: $error, first node on way shown",
        "icon": "coastline/error_line.png",
        "iconAnchor": [10, 10],
        "lon": $lon1,
        "lat": $lat1
EOF
			}
			$n++;
		}
		else
		{
			print STDERR "Unknown line: $geom,$osm_id,$error\n";
		}
	}
	$sth->finish();
}

# tidy up
print $OUTPUT "    },\n" unless( $n == 0 );
$dbh->disconnect();

# meta information
my $nowutc = strftime '%Y-%m-%d %H:%M UTC', gmtime;
print $OUTPUT <<EOF;
    {
        "control": "InfoBox",
        "text": "Coastline check completed at <b>$nowutc</b>, from db $dbcreated",
		"started" : "$startedutc"
    }
EOF

# finish up and move file
print $OUTPUT "]\n";
close $OUTPUT;
print "Moving $temp_dest to $DST_FILE\n";
system "mv $temp_dest $DST_FILE";

