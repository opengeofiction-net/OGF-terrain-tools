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

sub exportOverpassConvert($$);
sub buildOverpassQuery($$);
sub fileExport_Overpass($$);

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

# build up Overpass query to get the top level admin_level=0 continent relations
my $ADMIN_CONTINENT_QUERY = '[timeout:1800][maxsize:4294967296];((relation["type"="boundary"]["boundary"="administrative"]["admin_level"="0"]["ogf:id"~"^[A-Z]{2}$"];);>;);out;';

my $osmFile = $OUTPUT_DIR . '/continent_polygons.osm';
print "QUERY: $ADMIN_CONTINENT_QUERY\n";
fileExport_Overpass $osmFile, $ADMIN_CONTINENT_QUERY;
if( -f $osmFile and (stat $osmFile)[7] > 12000 )
{
	# load in continent relations
	my $ctx = OGF::Data::Context->new();
	$ctx->loadFromFile( $osmFile );
	$ctx->setReverseInfo();

	# for each continent
	foreach my $rel ( sort values %{$ctx->{_Relation}} )
	{
		print $rel->{'tags'}{'ogf:id'} . "\n";
		next if( $rel->{'tags'}{'ogf:id'} ne 'BG' );
		
		my($rc, $started, $pbfFile) = exportOverpassConvert \$ctx, \$rel;
		print "XX: $rc, $started, $pbfFile\n";
	}
	print "complete\n";
	exit 0;
}
else
{
	print "Error querying overpass\n";
	exit 1;
}

sub exportOverpassConvert($$)
{
	my($ctxref, $relref) = @_;
	my $continent = $$relref->{'tags'}{'ogf:id'};
	my $now       = time;
	my $started   = strftime '%Y%m%d%H%M%S', gmtime $now;
	my $startedat = strftime '%Y-%m-%d %H:%M:%S UTC', gmtime $now;
	my $osmFile   = "$OUTPUT_DIR/coastline-$continent-$started.osm";
	my $pbfFile   = "$OUTPUT_DIR/coastline-$continent-$started.osm.pbf";
	my $pubFile   = "$PUBLISH_DIR/coastline-$continent.osm.pbf" if( $PUBLISH_DIR );
	
	print "*** $continent *** $startedat **************************\n";
	print "* rel:", $$relref->{'id'}, ", continent:", $continent, "\n";
	
	my $overpass = buildOverpassQuery $ctxref, $relref;
	print "* query: $overpass\n";
	print "* query Overpass and save to: $osmFile\n";
	fileExport_Overpass $osmFile, $overpass;
	if( -f $osmFile and (stat $osmFile)[7] > 1000000 )
	{
		# convert to pbf
		print "* convert to: $pbfFile\n";
		system "osmium sort --no-progress --output=$pbfFile $osmFile";
		
		# and copy for web
		if( -f $pbfFile )
		{
			if( $pubFile )
			{
				print "* publish to: $pubFile\n";
				copy $pbfFile, $pubFile;
			}
			return 'success', $startedat, $pbfFile;
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

sub fileExport_Overpass($$)
{
	my( $outFile, $query ) = @_;

    my $data = OGF::Util::Overpass::runQuery_remote( undef, $query );
	if( !defined $data or $data !~ /^<\?xml/ )
	{
		print STDERR "Failure running Overpass query: $query\n";
		return;
	}
	#my $len = length $data;
	#if( length $data < 100 and 
	#print "DATA: $len $data\n";
	OGF::Util::File::writeToFile( $outFile, $data, '>:encoding(UTF-8)' );
}


