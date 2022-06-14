#! /usr/bin/perl -w
# 

use lib '/opt/opengeofiction/OGF-terrain-tools/lib';
use strict;
use warnings;
use File::Copy;
use OGF::Data::Context;
use OGF::Util::File;
use OGF::Util::Overpass;
use OGF::Util::Usage qw( usageInit usageError );
use POSIX;
use JSON::PP;

sub exportOverpassConvert($$$);
sub buildOverpassQuery($$);
sub fileExport_Overpass($$$);
sub validateCoastline($$$$);
sub validateCoastlineDb($$);

# parse options
my %opt;
usageInit( \%opt, qq/ h od=s copyto=s /, << "*" );
[-od <output_directory>] [-copyto <publish_directory>]

-od     Location to output JSON files
-copyto Location to publish JSON files for wiki & other use
*
usageError() if $opt{'h'};

my $OUTPUT_DIR  = ($opt{'od'} and -d $opt{'od'}) ? $opt{'od'} : '/tmp';
my $PUBLISH_DIR = ($opt{'copyto'} and -d $opt{'copyto'}) ? $opt{'copyto'} : undef;

my $OSMCOASTLINE = '/opt/opengeofiction/osmcoastline/bin/osmcoastline';
$OSMCOASTLINE = 'osmcoastline' if( ! -x $OSMCOASTLINE );

# build up Overpass query to get the top level admin_level=0 continent relations
my $ADMIN_CONTINENT_QUERY = '[timeout:1800][maxsize:4294967296];((relation["type"="boundary"]["boundary"="administrative"]["admin_level"="0"]["ogf:id"~"^[A-Z]{2}$"];);>;);out;';

my $osmFile = $OUTPUT_DIR . '/continent_polygons.osm';
print "QUERY: $ADMIN_CONTINENT_QUERY\n";
fileExport_Overpass $osmFile, $ADMIN_CONTINENT_QUERY, 12000;
if( -f $osmFile )
{
	# load in continent relations
	my $ctx = OGF::Data::Context->new();
	$ctx->loadFromFile( $osmFile );
	$ctx->setReverseInfo();

	# save coastline errors
	my @errs;
	
	# for each continent
	foreach my $rel ( values %{$ctx->{_Relation}} )
	{
		my $now       = time;
		my $started   = strftime '%Y%m%d%H%M%S', gmtime $now;
		my $startedat = strftime '%Y-%m-%d %H:%M:%S UTC', gmtime $now;
		my $continent = $rel->{'tags'}{'ogf:id'};
		my $relid     = $rel->{'id'};
		
		print "\n*** $continent ** $relid ** $startedat **************************\n";
		next if( $continent ne 'AR' ); # testing
		
		# get osm coastline data via overpass and convert to osm.pbf
		my($rc, $pbfFile) = exportOverpassConvert \$ctx, \$rel, $started;
		unless( $rc eq 'success' )
		{
			print "unable to download $continent coastline, will use last good\n";
			next;
		}
		
		# run osmcoastline to validate
		my $dbFile   = "$OUTPUT_DIR/coastline-$continent-$started.db";
		unless( validateCoastline(\@errs, $pbfFile, $dbFile, 'quick') == 0 )
		{
			print "issues with $continent coastline, will use last good\n";
			next;
		}
	}
	
	# save errors to JSON
	
	my $jsonFile = $OUTPUT_DIR . '/coastline_errors.json';
	my $json = JSON::PP->new->canonical->indent(2)->space_after;
	my $text = $json->encode( \@errs );
	OGF::Util::File::writeToFile($jsonFile, $text, '>:encoding(UTF-8)' );
	
	print "complete\n";
	exit 0;
}
else
{
	print "Error querying overpass\n";
	exit 1;
}

sub exportOverpassConvert($$$)
{
	my($ctxref, $relref, $started) = @_;
	my $continent = $$relref->{'tags'}{'ogf:id'};
	my $osmFile   = "$OUTPUT_DIR/coastline-$continent-$started.osm";
	my $pbfFile   = "$OUTPUT_DIR/coastline-$continent-$started.osm.pbf";
	my $pubFile   = "$PUBLISH_DIR/coastline-$continent.osm.pbf" if( $PUBLISH_DIR );
	
	my $overpass = buildOverpassQuery $ctxref, $relref;
	print "query: $overpass\n";
	print "query Overpass and save to: $osmFile\n";
	fileExport_Overpass $osmFile, $overpass, 90000;
	if( -f $osmFile )
	{
		# convert to pbf
		print "convert to: $pbfFile using osmium sort\n";
		system "osmium sort --no-progress --output=$pbfFile $osmFile";
		
		# and copy for web
		if( -f $pbfFile )
		{
			unlink $osmFile;
			if( $pubFile )
			{
				print "* publish to: $pubFile\n";
				copy $pbfFile, $pubFile;
			}
			return 'success', $pbfFile;
		}
	}
	return 'fail';
}

sub buildOverpassQuery($$)
{
	my($ctxref, $relref) = @_;
	my $overpass = qq|[out:xml][timeout:1800][maxsize:4294967296];(|;
	
	# query all coastlines within the continent using the extracted latlons
	# to limit - normally you'd use the built in overpass support for area
	# filters, but that does not work with the OGF setup
	my $aRelOuter = $$relref->closedWayComponents('outer');
	foreach my $way ( @$aRelOuter )
	{
		my $latlons = '';
		foreach my $nodeId ( @{$way->{'nodes'}} )
		{
			my $node = $$ctxref->{_Node}{$nodeId};
			if( ! $node )
			{
				print STDERR "  invalid node $nodeId (possible Overpass problem)\n";
				next;
			}
			$latlons .= ' ' if( $latlons ne '' );
			$latlons .= $node->{'lat'} . ' ' . $node->{'lon'};
		}
		$overpass .= qq|way["natural"="coastline"](poly:"$latlons");|;
	}
	$overpass .= qq|);(._;>;);out;|;
}

sub fileExport_Overpass($$$)
{
	my($outFile, $query, $minSize) = @_;
	
	my $retries = 0;
	while( ++$retries <= 10 )
	{
		sleep 3 * $retries if( $retries > 1 );
		my $data = OGF::Util::Overpass::runQuery_remote( undef, $query );
		if( !defined $data or $data !~ /^<\?xml/ )
		{
			print "Failure running Overpass query [$retries]: $query\n";
			next;
		}
		elsif( length $data < $minSize )
		{
			my $first400 = substr $data, 0, 400;
			my $len = length $data;
			print "Failure running Overpass query, return too small $len [$retries]: $first400\n";
			next;
		}
		
		OGF::Util::File::writeToFile( $outFile, $data, '>:encoding(UTF-8)' );
		return;
	}
}

sub validateCoastline($$$$)
{
	my($errs, $pbfFile, $dbFile, $mode) = @_;
	my $exotics = 0; my $warnings = 0; my $errors = 0;
	
	my $cmd = "$OSMCOASTLINE --verbose --srs=3857 --output-lines --output-polygons=both --output-rings --max-points=2000 --output-database=$dbFile $pbfFile 2>&1";
	$cmd = "$OSMCOASTLINE --verbose --srs=3857 --max-points=0 --output-database=$dbFile $pbfFile 2>&1" if( $mode eq 'quick' );
	open(my $pipe, '-|', $cmd) or return -1;
	while( my $line = <$pipe> )
	{
		print $line;
		if( $line =~ /Hole lies outside shell at or near point ([-+]?\d*\.?\d+) ([-+]?\d*\.?\d+)/ )
		{
			my %err = ();
			$err{'text'} = "Hole lies outside shell";
			$err{'icon'} = 'red';
			$err{'lat'} = $2; $err{'lon'} = $1;
			push @$errs, \%err;
			print "EXOTIC ERROR at: $1 $2\n";
			$exotics += 1;
		}
		$warnings = $1 if( $line =~ /There were (\d+) warnings/ );
		$errors = $1 if( $line =~ /There were (\d+) errors/ );
	}
	close $pipe;
	
	my $issues = $warnings + $errors + $exotics;
	print "$issues issues (warnings: $warnings; errors: $errors; exotics: $exotics)\n";
	
	validateCoastlineDb $errs, $dbFile;
	
	return $issues;
}

sub validateCoastlineDb($$)
{
	my($errs, $dbFile) = @_;
	
	# add WGS84 to SRIDs
	unless( `echo 'SELECT srid FROM spatial_ref_sys WHERE srid=4326;' | spatialite $dbFile ` =~ /^4326/ )
	{
		open my $sql, '|-', "spatialite $dbFile | cat";
		print $sql qq{INSERT INTO spatial_ref_sys(srid,auth_name,auth_srid,ref_sys_name,proj4text,srtext) VALUES (4326, 'epsg', 4326, 'WGS 84', '+proj=longlat +datum=WGS84 +no_defs', 'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],AXIS["Latitude",NORTH],AXIS["Longitude",EAST],AUTHORITY["EPSG","4326"]]');};
		close $sql;
	}
	
	# error_points
	print "checking points...\n";
	my %nodes = ();
	foreach my $line( `echo "SELECT AsText(Transform(GEOMETRY,4326)) AS geom, osm_id, error FROM error_points;"| spatialite $dbFile `)
	{
		chomp $line;
		my($geom, $osm_id, $error) = split /\|/, $line;
		my $sub = substr $geom, 0, 70;
		printf "P: %-70s / %s / %s\n", $sub, $osm_id, $error;
		
		if( $geom =~ /^POINT\(([\-\d]+\.[\d]+) ([\-\d]+\.[\d]+)\)$/ )
		{
			my $lat = $2; my $lon = $1;
			$nodes{"$lat:$lon"} = 1; # use later to avoid duplicate error_line outputs
			
			if( $error eq 'tagged_node' )
			{
				my %err = ();
				$err{'text'} = "Node $osm_id has natural=coastline property tags";
				$err{'icon'} = "coastline/$error.png"; $err{'iconAnchor'} = [10, 10];
				$err{'lat'} = $lat; $err{'lon'} = $lon;
				$err{'id'} = $osm_id;
				push @$errs, \%err;
			}
			elsif( $error eq 'intersection' )
			{
				my %err = ();
				$err{'text'} = "Intersection of coastline ways";
				$err{'icon'} = "coastline/$error.png"; $err{'iconAnchor'} = [10, 10];
				$err{'lat'} = $lat; $err{'lon'} = $lon;
				push @$errs, \%err;
			}
			elsif( $error eq 'not_a_ring' )
			{
				my %err = ();
				$err{'text'} = "Not a ring: coastline could not be constructed into a closed polygon";
				$err{'icon'} = "coastline/$error.png"; $err{'iconAnchor'} = [10, 10];
				$err{'lat'} = $lat; $err{'lon'} = $lon;
				push @$errs, \%err;
			}
			elsif( $error eq 'unconnected' or $error eq 'fixed_end_point' )
			{
				my %err = ();
				$err{'text'} = "$error: Coastline is not closed";
				$err{'icon'} = "coastline/unconnected.png"; $err{'iconAnchor'} = [10, 10];
				$err{'lat'} = $lat; $err{'lon'} = $lon;
				push @$errs, \%err;
			}
			elsif( $error eq 'double_node' )
			{
				my %err = ();
				$err{'text'} = "Node $osm_id appears more than once in coastline";
				$err{'icon'} = "coastline/$error.png"; $err{'iconAnchor'} = [10, 10];
				$err{'lat'} = $lat; $err{'lon'} = $lon;
				$err{'id'} = $osm_id;
				push @$errs, \%err;
			}
			else
			{
				my %err = ();
				$err{'text'} = "Error: $error";
				$err{'icon'} = 'red';
				$err{'lat'} = $lat; $err{'lon'} = $lon;
				push @$errs, \%err;
				print "UNKNOWN: $geom,$osm_id,$error\n";
			}
		}
	}
	# error_lines
	print "checking lines...\n";
	foreach my $line( `echo "SELECT AsText(Transform(GEOMETRY,4326)) AS geom, osm_id, error FROM error_lines;"| spatialite $dbFile `)
	{
		chomp $line;
		my($geom, $osm_id, $error) = split /\|/, $line;
		my $sub = substr $geom, 0, 70;
		printf "L: %-70s / %d / %s\n", $sub, $osm_id, $error;
		
		if( $geom =~ /^LINESTRING\(([\-\d]+\.[\d]+) ([\-\d]+\.[\d]+)/ )
		{
			my $lat = $2; my $lon = $1;
			next if( exists $nodes{"$lat:$lon"} ); # don't output if we already had a node report
			
			if( $error eq 'overlap' )
			{
				my %err = ();
				$err{'text'} = "Overlapping coastline, first node on way shown";
				$err{'icon'} = "coastline/$error.png"; $err{'iconAnchor'} = [10, 10];
				$err{'lat'} = $lat; $err{'lon'} = $lon;
				push @$errs, \%err;
			}
			elsif( $error eq 'direction' )
			{
				my %err = ();
				$err{'text'} = "Reversed coastline - should be counter-clockwise, first node on way shown";
				$err{'icon'} = "coastline/wrong_direction.png"; $err{'iconAnchor'} = [10, 10];
				$err{'lat'} = $lat; $err{'lon'} = $lon;
				push @$errs, \%err;
			}
			else
			{
				my %err = ();
				$err{'text'} = "Error lines: $error, first node on way shown";
				$err{'icon'} = "coastline/error_line.png"; $err{'iconAnchor'} = [10, 10];
				$err{'lat'} = $lat; $err{'lon'} = $lon;
				push @$errs, \%err;
			}
		}
		else
		{
			print "UNKNOWN: $geom,$osm_id,$error\n";
		}
	}
}
