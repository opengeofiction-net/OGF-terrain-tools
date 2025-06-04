#! /usr/bin/perl -w

use lib '/opt/opengeofiction/OGF-terrain-tools/lib';
use strict;
use warnings;
use feature 'unicode_strings' ;
use Date::Format;
use Encode;
use JSON::XS;
use OGF::Util::File;
use OGF::Util::Overpass;
use OGF::Util::Usage qw( usageInit usageError );

sub parseRef($$);
sub parseContinent($$);
sub parseSector($);
sub parseScope($);
sub parsePermission($);
sub fileExport_Overpass($);
sub housekeeping($$);

binmode(STDOUT, ":utf8");

my %opt;
usageInit( \%opt, qq/ h ogf ds=s od=s copyto=s /, << "*" );
[-ds <dataset>] [-od <output_directory>] [-copyto <publish_directory>]

-ds     "test" or empty
-od     Location to output JSON files
-copyto Location to publish JSON files for wiki use
*

my( $jsonFile ) = @ARGV;
usageError() if $opt{'h'};

my $URL_TERRITORIES = 'https://wiki.opengeofiction.net/index.php/OpenGeofiction:Territory_administration?action=raw';
my $OUTPUT_DIR  = $opt{'od'}     || '/tmp';
my $PUBLISH_DIR = $opt{'copyto'} || '/tmp';
my $OUTFILE_NAME = 'airports';
my $QUERY;

housekeeping $OUTPUT_DIR, time;

if( ! $opt{'ds'} )
{
	$OUTFILE_NAME = 'airports';
	# query takes ~ 2s, returning ~ 0.1 MB; allow up to 20s, 2 MB
	$QUERY = << '---EOF---';
[timeout:300][maxsize:2000000][out:json];
area[type=boundary][boundary=administrative][admin_level=2]["ogf:id"]->.territories;
foreach.territories->.territory(
  .territory out tags;
  wr[aeroway=aerodrome][name](area.territory)->.airports;
  .airports map_to_area->.airportsarea;
  foreach.airportsarea->.airport(
    wr(pivot.airport)->.airportobj;
    .airportobj out tags center;
    way(area.airport)[aeroway=runway][ref](if: is_closed() == 0);
    out tags center;
  );
);
---EOF---
}
elsif( $opt{'ds'} eq 'test' )
{
	$OUTFILE_NAME .= '_test';
	# query takes ~ 2s, returning ~ 0.1 MB; allow up to 20s, 2 MB
	$QUERY = << '---EOF---';
[timeout:20][maxsize:2000000][out:json];
area[type=boundary][boundary=administrative][admin_level=2]["ogf:id"~"^(BG01|AR120|UL05[ab])$"]->.territories;
foreach.territories->.territory(
  .territory out tags;
  wr[aeroway=aerodrome][name](area.territory)->.airports;
  .airports map_to_area->.airportsarea;
  foreach.airportsarea->.airport(
    wr(pivot.airport)->.airportobj;
    .airportobj out tags center;
    way(area.airport)[aeroway=runway][ref](if: is_closed() == 0);
    out tags center;
  );
);
---EOF---
}
else
{
	die qq/Unknown dataset: "$opt{ds}"/;
}

# an .json file can be specified as the last commandline argument, otherwise get from Overpass
if( ! $jsonFile )
{
	$jsonFile = $OUTPUT_DIR . '/' . $OUTFILE_NAME . '_'. time2str('%Y%m%d%H%M%S', time) . '.json';
	fileExport_Overpass $jsonFile if( ! -f $jsonFile );
}
exit if( ! -f $jsonFile );

# and now load it in
my $results = undef;
if( open( my $fh, '<', $jsonFile ) )
{
	my $json = JSON::XS->new->utf8();
	my $file_content = do { local $/; <$fh> };
	eval { $results = $json->decode($file_content); 1; }
}
die qq/Cannot load JSON from Overpass/ if( !defined $results );

# load in the territories JSON
print "loading territories...\n";
my $userAgent = LWP::UserAgent->new(keep_alive => 20, agent => 'OGF-overpassCacheAirports.pl/2025.06');
my $resp = $userAgent->get($URL_TERRITORIES);
die qq/Cannot read $URL_TERRITORIES/ unless( $resp->is_success );
my $territories = JSON::XS->new->utf8->decode ($resp->content);

# build up a list of canonical territories
print "selecting canonical territories...\n";
my %canonicalTerritories;
foreach my $territory ( @$territories )
{
	my($ogfId, $status) = ($territory->{ogfId}, $territory->{status});
	if( $ogfId =~ /^BG/ )
	{
		# not canonical - beginner territory
	}
	elsif( $status =~ /^(available|reserved)$/ )
	{
		# not canonical - inactive
	}
	elsif( $status =~ /^(owned|collaborative|archived|open to all|outline|marked for withdrawal)$/ )
	{
		$canonicalTerritories{$ogfId} = $status;
	}
	else
	{
		print "> unexpected territory status: $ogfId $status\n";
	}
}

# for each item in the Overpass results
my @out;
my %refs;
my $records = $results->{elements};
my %currentTerritory;
print "parsing airports JSON...\n";
for my $record ( @$records )
{
	my %entry = ();

	# if we don't have a name, you're not getting in
	my $id = substr($record->{type}, 0, 1) . $record->{id};
	
	# is this a territory?
	if( exists $record->{tags}->{boundary} and $record->{tags}->{boundary} eq 'administrative' )
	{
		%currentTerritory = ();
		
		# is the territory canonical?
		if( exists $canonicalTerritories{$record->{tags}->{'ogf:id'}} )
		{
			$currentTerritory{'ogf:id'}             = $record->{'tags'}{'ogf:id'};
			$currentTerritory{'is_in:country'}      = $record->{'tags'}{'int_name'} || $record->{'tags'}{'name'} || $record->{'tags'}{'ogf:id'};
			$currentTerritory{'is_in:country:wiki'} = $record->{'tags'}{'ogf:wiki'} || $record->{'tags'}{'ogfwiki'} || $currentTerritory{'is_in:country'};
			$currentTerritory{'is_in:continent'}    = parseContinent $record->{'tags'}{'is_in:continent'}, $record->{'tags'}{'ogf:id'};
	
			print "> parsing airports in $canonicalTerritories{$record->{tags}->{'ogf:id'}} $record->{tags}->{'ogf:id'}: $record->{tags}->{name}\n";
		}
		else
		{
			print "> SKIPPING airports in non-canonical $record->{tags}->{'ogf:id'}: $record->{tags}->{name}\n";
		}
	}
	# is this an airport?
	elsif( exists $record->{tags}->{aeroway} and $record->{tags}->{aeroway} eq 'aerodrome' )
	{
		# valid territory?
		next if( !exists $currentTerritory{'ogf:id'} );
		
		$entry{'is_in:ogfId'}        = $currentTerritory{'ogf:id'};
		$entry{'is_in:continent'}    = $currentTerritory{'is_in:continent'};
		$entry{'is_in:country'}      = $currentTerritory{'is_in:country'};
		$entry{'is_in:country:wiki'} = $currentTerritory{'is_in:country:wiki'};
		$entry{'id'}                 = $record->{id};
		$entry{'type'}               = $record->{type};
		$entry{'name'}               = $record->{tags}->{name};
		$entry{'lat'}                = $record->{center}->{lat};
		$entry{'lon'}                = $record->{center}->{lon};
		
		# parse the airport ref, and check unique
		$entry{'ref'} = parseRef $record->{tags}->{ref}, $record->{tags}->{iata};
		if( !defined $entry{'ref'} )
		{
			#print "$entry{'is_in:ogfId'},$id,$entry{'name'} --> invalid ref\n";
			next;
		}
		$entry{'ref'} = uc $entry{'ref'};
		if( exists $refs{$entry{'ref'}} )
		{
			#print "$entry{'is_in:ogfId'},$id,$entry{'name'} --> duplicate ref: $entry{'ref'}\n";
			next;
		}
		push @out, \%entry;
		print "$entry{'is_in:ogfId'},$id,$entry{'name'},$entry{ref}\n";
		$refs{$entry{'ref'}} = $entry{'ref'};
	}
	# is this an runway?
	elsif( exists $record->{tags}->{aeroway} and $record->{tags}->{aeroway} eq 'runway' )
	{
	}
	else
	{
		next;
	}
}

# create output file
my $publishFile = $PUBLISH_DIR . '/' . $OUTFILE_NAME . '.json';
my $json = JSON::XS->new->canonical->indent(2)->space_after;
my $text = $json->encode( \@out );
OGF::Util::File::writeToFile($publishFile, $text, '>:encoding(UTF-8)' );

#-------------------------------------------------------------------------------
sub parseRef($$)
{
	my($var1, $var2) = @_;
	my $ref = $var1 || $var2 || undef;
	return $ref if( defined $ref and $ref =~ /^[A-Z]{3}$/ );
	undef;
}

#-------------------------------------------------------------------------------
sub parseContinent($$)
{
	my($cont, $ogfId) = @_;
	
	return 'Unknown' if( !defined $cont and !defined $ogfId );
	return $cont if( defined $cont and $cont =~ /^(Antarephia|Beginner|East Uletha|Ereva|Kartumia|North Archanta|Orano|Pelanesia|South Archanta|Tarephia|West Uletha)$/ );
	return 'Antarephia' if( defined $ogfId and $ogfId =~ /^AN/ );
	#return 'Archanta' if( defined $ogfId and $ogfId =~ /^AR/ );
	return 'Beginner' if( defined $ogfId and $ogfId =~ /^BG/ );
	return 'Ereva' if( defined $ogfId and $ogfId =~ /^ER/ );
	return 'Kartumia' if( defined $ogfId and $ogfId =~ /^KA/ );
	return 'Orano' if( defined $ogfId and $ogfId =~ /^OR/ );
	return 'Pelanesia' if( defined $ogfId and $ogfId =~ /^PE/ );
	return 'Tarephia' if( defined $ogfId and $ogfId =~ /^TA/ );
	return 'West Uletha' if( defined $ogfId and $ogfId =~ /^UL(\d\d)/ and $1 <= 17 );
	return 'East Uletha' if( defined $ogfId and $ogfId =~ /^UL(\d\d)/ and $1 >= 18 );
	return 'Unknown';
}

#-------------------------------------------------------------------------------
sub fileExport_Overpass($)
{
	my($outFile) = @_;

	my $data = decode('utf-8', OGF::Util::Overpass::runQuery_remoteRetryOptions(undef, $QUERY, 32, 'json', 3, 3));
	OGF::Util::File::writeToFile( $outFile, $data, '>:encoding(UTF-8)' ) if( defined $data );
}

#-------------------------------------------------------------------------------
sub housekeeping($$)
{
	my($dir, $now) = @_;
	my $KEEP_FOR = 60 * 60 * 6 ; # 6 hours
	my $dh;
	
	opendir $dh, $dir;
	while( my $file = readdir $dh )
	{
		next unless( $file =~ /^economy_\d{14}\.json/ );
		if( $now - (stat "$dir/$file")[9] > $KEEP_FOR )
		{
			print "deleting: $dir/$file\n";
			unlink "$dir/$file";
		}
	}
	closedir $dh;
}
