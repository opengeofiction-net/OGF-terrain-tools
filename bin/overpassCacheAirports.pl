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

sub addAirport($);
sub parseRef($$);
sub parseStr($$$$);
sub parseContinent($$);
sub parsePermission($);
sub parseAerodromeType($);
sub fileExport_Overpass($);
sub housekeeping($$$);

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
    out tags;
    node(area.airport)[aeroway=gate];
    out tags;
    nwr(area.airport)[aeroway=terminal];
    out tags;
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
    out tags;
    node(area.airport)[aeroway=gate];
    out tags;
    nwr(area.airport)[aeroway=terminal];
    out tags;
  );
);
---EOF---
}
else
{
	die qq/Unknown dataset: "$opt{ds}"/;
}

housekeeping $OUTPUT_DIR, $OUTFILE_NAME, time;

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
die qq/Overpass runtime error: $results->{remark}/ if( $results->{remark} and $results->{remark} =~ /^runtime error/ );

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
my @errors;
my %refs;
my $records = $results->{elements};
my %currentTerritory;
my $entry = {};
print "parsing airports JSON...\n";
for my $record ( @$records )
{
	my $id = substr($record->{type}, 0, 1) . $record->{id};
	
	# is this a territory?
	if( exists $record->{tags}->{boundary} and $record->{tags}->{boundary} eq 'administrative' )
	{
		# current airport entry to flush out?
		addAirport $entry; $entry = {};
		
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
		# current airport entry to flush out?
		addAirport $entry; $entry = {};
		
		# valid territory?
		next if( !exists $currentTerritory{'ogf:id'} );
		
		$entry->{'ref'}                = parseRef $record->{tags}->{ref}, $record->{tags}->{iata};
		$entry->{'ogf:id'}             = $currentTerritory{'ogf:id'};
		$entry->{'is_in:continent'}    = $currentTerritory{'is_in:continent'};
		$entry->{'is_in:country'}      = $currentTerritory{'is_in:country'};
		$entry->{'is_in:country:wiki'} = $currentTerritory{'is_in:country:wiki'};
		$entry->{'id'}                 = $id;
		$entry->{'name'}               = $record->{tags}->{name};
		$entry->{'description'}        = parseStr $record->{tags}->{description}, undef, '', 100;
		$entry->{'serves'}             = parseStr $record->{tags}->{serves}, $record->{tags}->{'is_in:city'}, '', undef;
		$entry->{'lat'}                = $record->{center}->{lat};
		$entry->{'lon'}                = $record->{center}->{lon};
		$entry->{'ogf:logo'}           = $record->{tags}->{'ogf:logo'} || 'Question mark in square brackets.svg';
		$entry->{'ogf:permission'}     = parsePermission $record->{tags}->{'ogf:permission'};
		$entry->{'type'}               = parseAerodromeType $record->{tags}->{'aerodrome:type'};
		$entry->{'runways'}            = ();
		$entry->{'runways:count'}      = 0;
		$entry->{'gates:count'}        = 0;
		$entry->{'terminals:count'}    = 0;
		$entry->{'terminals'}          = ();
	}
	# is this an runway?
	elsif( exists $entry->{'ogf:id'} and exists $record->{tags}->{aeroway} and $record->{tags}->{aeroway} eq 'runway' )
	{
		if( $record->{tags}->{ref} =~ /^(0?[1-9]|[1-2]\d|3[0-6])[LCR]?(\/(0?[1-9]|[1-2]\d|3[0-6])[LCR]?)?$/ )
		{
			my $runway = {};
			$runway->{'ref'}     = $record->{tags}->{ref};
			$runway->{'width'}   = $record->{tags}->{width} || 45;
			$runway->{'length'}  = $record->{tags}->{length} || '';
			$runway->{'surface'} = $record->{tags}->{surface} || 'concrete';
			
			$entry->{'runways:count'}++;
			push @{$entry->{'runways'}}, $runway;
		}
	}
	# is this an gate?
	elsif( exists $entry->{'ogf:id'} and exists $record->{tags}->{aeroway} and $record->{tags}->{aeroway} eq 'gate' )
	{
		$entry->{'gates:count'}++;
	}
	# is this an terminal?
	elsif( exists $entry->{'ogf:id'} and exists $record->{tags}->{aeroway} and $record->{tags}->{aeroway} eq 'terminal' )
	{
		my $name = parseStr $record->{tags}->{name}, $record->{tags}->{ref}, undef, 30;
		if( defined $name )
		{
			$entry->{'terminals:count'}++;
			push @{$entry->{'terminals'}}, $name;
		}
	}
	else
	{
	}
}

# current airport entry to flush out?
addAirport $entry; $entry = {};

# create output files
my $publishFile = $PUBLISH_DIR . '/' . $OUTFILE_NAME . '.json';
my $json = JSON::XS->new->canonical->indent(2)->space_after;
my $text = $json->encode( \@out );
OGF::Util::File::writeToFile($publishFile, $text, '>:encoding(UTF-8)' );
$publishFile = $PUBLISH_DIR . '/' . $OUTFILE_NAME . '_errors.json';
$json = JSON::XS->new->canonical->indent(2)->space_after;
$text = $json->encode( \@errors );
OGF::Util::File::writeToFile($publishFile, $text, '>:encoding(UTF-8)' );

#-------------------------------------------------------------------------------
sub addAirport($)
{
	my($entry) = @_;
	if( defined $entry and exists $entry->{'ogf:id'} )
	{
		# check ref, and check unique
		if( !defined $entry->{'ref'} )
		{
			push @errors, {'ogf:id' => $entry->{'ogf:id'}, 'id' => $entry->{'id'}, 'name' => $entry->{'name'}, 'text' => "invalid ref"};
			return;
		}
		$entry->{'ref'} = uc $entry->{'ref'};
		if( exists $refs{$entry->{'ref'}} )
		{
			push @errors, {'ogf:id' => $entry->{'ogf:id'}, 'id' => $entry->{'id'}, 'name' => $entry->{'name'}, 'text' => "duplicate ref: $entry->{'ref'}"};
			return;
		}
		
		# don't include every type of aerodrome
		if( $entry->{'type'} ne 'international' and $entry->{type} ne 'regional' )
		{
			push @errors, {'ogf:id' => $entry->{'ogf:id'}, 'id' => $entry->{'id'}, 'name' => $entry->{'name'}, 'text' => "skipping aerodrome:type=$entry->{'type'}"};
			return;
		}
		if( defined $entry->{'military'} )
		{
			push @errors, {'ogf:id' => $entry->{'ogf:id'}, 'id' => $entry->{'id'}, 'name' => $entry->{'name'}, 'text' => "skipping military=airfield"};
			return;
		}
		
		# ensure at least 1 runway
		if( $entry->{'runways:count'} < 1 )
		{
			push @errors, {'ogf:id' => $entry->{'ogf:id'}, 'id' => $entry->{'id'}, 'name' => $entry->{'name'}, 'text' => "no runways found"};
			return;
		}
		
		# ensure at least 1 terminal
		if( $entry->{'terminals:count'} < 1 )
		{
			push @errors, {'ogf:id' => $entry->{'ogf:id'}, 'id' => $entry->{'id'}, 'name' => $entry->{'name'}, 'text' => "no terminals found"};
			return;
		}
		
		push @out, $entry;
		print "OUT: $entry->{'ogf:id'},$entry->{id},$entry->{name},$entry->{ref}\n";
		$refs{$entry->{'ref'}} = $entry->{'ref'};
		$entry = {};
	}
}

#-------------------------------------------------------------------------------
sub parseRef($$)
{
	my($var1, $var2) = @_;
	my $ref = $var1 || $var2 || undef;
	return $ref if( defined $ref and $ref =~ /^[A-Z]{3}$/ );
	undef;
}

#-------------------------------------------------------------------------------
sub parseStr($$$$)
{
	my($var1, $var2, $var3, $max) = @_;
	my $ret = $var1 || $var2 || $var3;
	$ret = substr $ret, 0, $max if( defined $ret and defined $max );
	$ret;
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
sub parsePermission($)
{
	my($var1) = @_;
	return $var1 if( $var1 and ($var1 eq 'yes' or $var1 eq 'no' or $var1 eq 'ask') );
	return 'ask';
}

#-------------------------------------------------------------------------------
sub parseAerodromeType($)
{
	my($at) = @_;
	return 'regional' if( !defined $at );
	if( $at eq 'international'   or $at eq 'regional' or $at eq 'public'  or
	    $at eq 'gliding'         or $at eq 'airfield' or $at eq 'private' or
	    $at eq 'military/public' or $at eq 'military' )
	{
		return $at;
	}
	return 'regional';
}

#-------------------------------------------------------------------------------
sub fileExport_Overpass($)
{
	my($outFile) = @_;

	my $data = decode('utf-8', OGF::Util::Overpass::runQuery_remoteRetryOptions(undef, $QUERY, 32, 'json', 3, 3));
	OGF::Util::File::writeToFile( $outFile, $data, '>:encoding(UTF-8)' ) if( defined $data );
}

#-------------------------------------------------------------------------------
sub housekeeping($$$)
{
	my($dir, $prefix, $now) = @_;
	my $KEEP_FOR = 60 * 60 * 6 ; # 6 hours
	my $dh;
	
	opendir $dh, $dir;
	while( my $file = readdir $dh )
	{
		next unless( $file =~ /^${prefix}_\d{14}\.json/ );
		if( $now - (stat "$dir/$file")[9] > $KEEP_FOR )
		{
			print "deleting: $dir/$file\n";
			unlink "$dir/$file";
		}
	}
	closedir $dh;
}
