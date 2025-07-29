#! /usr/bin/perl -w

use lib '/opt/opengeofiction/OGF-terrain-tools/lib';
use strict;
use warnings;
use feature 'unicode_strings' ;
use utf8;
use Date::Format;
use Encode;
use JSON::XS;
use OGF::Util::File;
use OGF::Util::Overpass;
use OGF::Util::Usage qw( usageInit usageError );

sub addAirport($);
sub addAirline($);
sub parseAirportRef($$);
sub parseAirlineRef($);
sub parseStr($$$$);
sub parseContinent($$);
sub parsePermission($);
sub parseAerodromeType($);
sub parseLength($);
sub fileExport_Overpass($);
sub housekeeping($$$$);

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
my $URL_SETTINGS = 'https://wiki.opengeofiction.net/index.php/OpenGeofiction:Territory_administration/settings?action=raw';
my $OUTPUT_DIR  = $opt{'od'}     || '/tmp';
my $PUBLISH_DIR = $opt{'copyto'} || '/tmp';
my $OUTFILE_NAME_AIRPORTS = 'airports';
my $OUTFILE_NAME_AIRLINES = 'airlines';
my $QUERY;

if( ! $opt{'ds'} )
{
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
  nwr(area.territory)["headquarters"="main"]["economy:sector"="tertiary"]["economy:iclass"~"[Aa]irline"];
  out tags center;
);
---EOF---
}
elsif( $opt{'ds'} eq 'test' )
{
	$OUTFILE_NAME_AIRPORTS .= '_test';
	$OUTFILE_NAME_AIRLINES .= '_test';
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
  nwr(area.territory)["headquarters"="main"]["economy:sector"="tertiary"]["economy:iclass"~"[Aa]irline"];
  out tags center;
);
---EOF---
}
else
{
	die qq/Unknown dataset: "$opt{ds}"/;
}

housekeeping $OUTPUT_DIR, $OUTFILE_NAME_AIRPORTS, $OUTFILE_NAME_AIRLINES, time;

# an .json file can be specified as the last commandline argument, otherwise get from Overpass
if( ! $jsonFile )
{
	$jsonFile = $OUTPUT_DIR . '/' . $OUTFILE_NAME_AIRPORTS . '_'. time2str('%Y%m%d%H%M%S', time) . '.json';
	print "running Overpass query...\n";
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

# load JSON settings
my $userAgent = LWP::UserAgent->new(keep_alive => 20, agent => 'OGF-overpassCacheAirports.pl/2025.06');
my $resp = $userAgent->get($URL_SETTINGS);
die qq/Cannot read $URL_SETTINGS/ unless( $resp->is_success );
my $settings = JSON::XS->new->utf8->decode ($resp->content);

# load in the territories JSON
print "loading territories...\n";
$resp = $userAgent->get($URL_TERRITORIES);
die qq/Cannot read $URL_TERRITORIES/ unless( $resp->is_success );
my $territories = JSON::XS->new->utf8->decode ($resp->content);

# build up a list of canonical territories
print "selecting canonical territories...\n";
my %canonicalTerritories;
my %noncanon = map { $_ => 1 } @{$settings->{non_canon_free_to_edit}};
foreach my $territory ( @$territories )
{
	my($ogfId, $status) = ($territory->{ogfId}, $territory->{status});
	
	if( $ogfId =~ /^BG/ )
	{
		# not canonical - beginner
	}
	elsif( exists $noncanon{$ogfId} )
	{
		# explicitly not canonical
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
my @airportOut;
my @airportErrors;
my %airportRefs;
my @airlineOut;
my @airlineErrors;
my %airlineRefs;
my $records = $results->{elements};
my %currentTerritory;
my $entry = {};
print "parsing airports JSON...\n";
for my $record ( @$records )
{
	my $id = $record->{type} . '/' . $record->{id};
	
	# is this a territory?
	if( exists $record->{tags}->{boundary} and $record->{tags}->{boundary} eq 'administrative' )
	{
		# current airport entry to flush out?
		addAirport $entry; $entry = {};
		%currentTerritory = ();
		
		# check valid ogf:id
		if( !exists $record->{tags}->{'ogf:id'} )
		{
			print "> ERROR in $id\n";
		}
		# is the territory canonical?
		elsif( exists $canonicalTerritories{$record->{tags}->{'ogf:id'}} and $record->{tags}->{'ogf:id'} ne $record->{tags}->{'name'} )
		{
			$currentTerritory{'ogf:id'}             = $record->{'tags'}{'ogf:id'};
			$currentTerritory{'is_in:country'}      = $record->{'tags'}{'int_name'} || $record->{'tags'}{'name'} || $record->{'tags'}{'ogf:id'};
			$currentTerritory{'is_in:country:wiki'} = $record->{'tags'}{'ogf:wiki'} || $record->{'tags'}{'ogfwiki'} || $currentTerritory{'is_in:country'};
			$currentTerritory{'is_in:continent'}    = parseContinent $record->{'tags'}{'is_in:continent'}, $record->{'tags'}{'ogf:id'};
	
			print "> parsing airports in $canonicalTerritories{$record->{tags}->{'ogf:id'}} $record->{tags}->{'ogf:id'}: $record->{tags}->{name}\n";
		}
		elsif( exists $canonicalTerritories{$record->{tags}->{'ogf:id'}} and $record->{tags}->{'ogf:id'} eq $record->{tags}->{'name'} )
		{
			print "> SKIPPING airports in $record->{tags}->{'ogf:id'}: territory name not set\n";
		}
		else
		{
			print "> SKIPPING airports and airlines in non-canonical $record->{tags}->{'ogf:id'}: $record->{tags}->{name}\n";
		}
	}
	# is this an airport?
	elsif( exists $record->{tags}->{aeroway} and $record->{tags}->{aeroway} eq 'aerodrome' )
	{
		# current airport entry to flush out?
		addAirport $entry; $entry = {};
		
		# valid territory?
		next if( !exists $currentTerritory{'ogf:id'} );
		
		$entry->{'ref'}                = parseAirportRef $record->{tags}->{ref}, $record->{tags}->{iata};
		$entry->{'ogf:id'}             = $currentTerritory{'ogf:id'};
		$entry->{'is_in:continent'}    = $currentTerritory{'is_in:continent'};
		$entry->{'is_in:country'}      = $currentTerritory{'is_in:country'};
		$entry->{'is_in:country:wiki'} = $currentTerritory{'is_in:country:wiki'};
		$entry->{'id'}                 = $id;
		$entry->{'name'}               = $record->{tags}{int_name} || $record->{tags}->{name};
		$entry->{'description'}        = parseStr $record->{tags}->{description}, undef, '', 100;
		$entry->{'serves'}             = parseStr $record->{tags}->{serves}, $record->{tags}->{'is_in:city'}, '', undef;
		$entry->{'lat'}                = $record->{center}->{lat};
		$entry->{'lon'}                = $record->{center}->{lon};
		$entry->{'ogf:logo'}           = $record->{tags}->{'ogf:logo'} || 'Question mark in square brackets.svg';
		$entry->{'ogf:permission'}     = parsePermission $record->{tags}->{'ogf:permission'};
		$entry->{'type'}               = parseAerodromeType $record->{tags}->{'aerodrome:type'};
		$entry->{'runway'}             = '';
		$entry->{'runways'}            = ();
		$entry->{'runways:count'}      = 0;
		$entry->{'gates:count'}        = 0;
		$entry->{'terminals:count'}    = 0;
		$entry->{'terminals'}          = ();
	}
	# is this an runway?
	elsif( exists $entry->{'ogf:id'} and exists $record->{tags}->{aeroway} and $record->{tags}->{aeroway} eq 'runway' )
	{
		# exclude displaced thresholds
		next if( exists $record->{tags}->{runway} and $record->{tags}->{runway} eq 'displaced_threshold' );

		# silently support dashes in refs, used in error instead of /
		my $runwayRef = $record->{tags}->{ref};
		$runwayRef =~ tr/-/\//;

		if( $runwayRef =~ /^(0?[1-9]|[1-2]\d|3[0-6])[LCR]?(\/(0?[1-9]|[1-2]\d|3[0-6])[LCR]?)?$/ )
		{
			my $runway = {};
			$runway->{'runway:ref'} = $runwayRef;
			$runway->{'width'}      = $record->{tags}->{width} || 45;
			$runway->{'length'}     = parseLength $record->{tags}->{length};
			$runway->{'surface'}    = $record->{tags}->{surface} || 'concrete';
			
			# add to textual summary
			$entry->{'runway'} .= ', ' if( $entry->{'runway'} );
			$entry->{'runway'} .= $runwayRef;
			$entry->{'runway'} .= "\x{00a0}(" . $runway->{'length'} . 'm)' if( $runway->{'length'} );

			# save runway in the airport list
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
	# an airline?
	elsif( exists $record->{tags}->{'economy:iclass'} and $record->{tags}->{'economy:iclass'} =~ /[Aa]irline/ )
	{
		my $airline = {};
		$airline->{'ref'}                = parseAirlineRef $record->{tags}->{ref};
		$airline->{'name'}               = $record->{tags}{brand} || $record->{tags}->{name};
		$airline->{'ogf:id'}             = $currentTerritory{'ogf:id'};
		$airline->{'is_in:continent'}    = $currentTerritory{'is_in:continent'};
		$airline->{'is_in:country'}      = $currentTerritory{'is_in:country'};
		$airline->{'is_in:country:wiki'} = $currentTerritory{'is_in:country:wiki'};
		$airline->{'id'}                 = $id;
		$airline->{'lat'}                = $record->{lat} || $record->{center}->{lat};
		$airline->{'lon'}                = $record->{lon} || $record->{center}->{lon};
		$airline->{'ogf:logo'}           = $record->{tags}->{'ogf:logo'} || 'Question mark in square brackets.svg';
		$airline->{'ogf:permission'}     = parsePermission $record->{tags}->{'ogf:permission'};
		addAirline $airline;
	}
	else
	{
	}
}

# current airport entry to flush out?
addAirport $entry; $entry = {};

# create output files
my $publishFile = $PUBLISH_DIR . '/' . $OUTFILE_NAME_AIRPORTS . '.json';
my $json = JSON::XS->new->canonical->indent(2)->space_after;
my $text = $json->encode( \@airportOut );
OGF::Util::File::writeToFile($publishFile, $text, '>:encoding(UTF-8)' );
$publishFile = $PUBLISH_DIR . '/' . $OUTFILE_NAME_AIRPORTS . '_errors.json';
$json = JSON::XS->new->canonical->indent(2)->space_after;
$text = $json->encode( \@airportErrors );
OGF::Util::File::writeToFile($publishFile, $text, '>:encoding(UTF-8)' );
$publishFile = $PUBLISH_DIR . '/' . $OUTFILE_NAME_AIRLINES . '.json';
$json = JSON::XS->new->canonical->indent(2)->space_after;
$text = $json->encode( \@airlineOut );
OGF::Util::File::writeToFile($publishFile, $text, '>:encoding(UTF-8)' );
$publishFile = $PUBLISH_DIR . '/' . $OUTFILE_NAME_AIRLINES . '_errors.json';
$json = JSON::XS->new->canonical->indent(2)->space_after;
$text = $json->encode( \@airlineErrors );
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
			push @airportErrors, {'ogf:id' => $entry->{'ogf:id'}, 'id' => $entry->{'id'}, 'name' => $entry->{'name'}, 'text' => "invalid ref"};
			return;
		}
		$entry->{'ref'} = uc $entry->{'ref'};
		if( exists $airportRefs{$entry->{'ref'}} )
		{
			push @airportErrors, {'ogf:id' => $entry->{'ogf:id'}, 'id' => $entry->{'id'}, 'name' => $entry->{'name'}, 'text' => "duplicate ref: $entry->{'ref'}"};
			return;
		}
		
		# don't include every type of aerodrome
		if( $entry->{'type'} ne 'global' and $entry->{'type'} ne 'international' and $entry->{type} ne 'regional' )
		{
			push @airportErrors, {'ogf:id' => $entry->{'ogf:id'}, 'id' => $entry->{'id'}, 'name' => $entry->{'name'}, 'text' => "skipping aerodrome:type=$entry->{'type'}"};
			return;
		}
		if( defined $entry->{'military'} )
		{
			push @airportErrors, {'ogf:id' => $entry->{'ogf:id'}, 'id' => $entry->{'id'}, 'name' => $entry->{'name'}, 'text' => "skipping military=airfield"};
			return;
		}
		
		# ensure at least 1 runway
		if( $entry->{'runways:count'} < 1 )
		{
			push @airportErrors, {'ogf:id' => $entry->{'ogf:id'}, 'id' => $entry->{'id'}, 'name' => $entry->{'name'}, 'text' => "no runways found"};
			return;
		}
		
		# ensure at least 1 terminal
		if( $entry->{'terminals:count'} < 1 )
		{
			push @airportErrors, {'ogf:id' => $entry->{'ogf:id'}, 'id' => $entry->{'id'}, 'name' => $entry->{'name'}, 'text' => "no terminals found"};
			return;
		}
		
		push @airportOut, $entry;
		print "OUT airport: $entry->{'ogf:id'},$entry->{id},$entry->{name},$entry->{ref}\n";
		$airportRefs{$entry->{'ref'}} = $entry->{'ref'};
		$entry = {};
	}
}

#-------------------------------------------------------------------------------
sub addAirline($)
{
	my($entry) = @_;
	if( defined $entry and exists $entry->{'ogf:id'} )
	{
		# check ref, and check unique
		if( !defined $entry->{'ref'} )
		{
			push @airlineErrors, {'ogf:id' => $entry->{'ogf:id'}, 'id' => $entry->{'id'}, 'name' => $entry->{'name'}, 'text' => "invalid ref"};
			return;
		}
		$entry->{'ref'} = uc $entry->{'ref'};
		if( exists $airlineRefs{$entry->{'ref'}} )
		{
			push @airlineErrors, {'ogf:id' => $entry->{'ogf:id'}, 'id' => $entry->{'id'}, 'name' => $entry->{'name'}, 'text' => "duplicate ref: $entry->{'ref'}"};
			return;
		}
		
		push @airlineOut, $entry;
		print "OUT airline: $entry->{'ogf:id'},$entry->{id},$entry->{name},$entry->{ref}\n";
		$airlineRefs{$entry->{'ref'}} = $entry->{'ref'};
		$entry = {};
	}
}

#-------------------------------------------------------------------------------
sub parseAirportRef($$)
{
	my($var1, $var2) = @_;
	my $ref = $var1 || $var2 || undef;
	return $ref if( defined $ref and $ref =~ /^[A-Z]{3}$/ );
	undef;
}

#-------------------------------------------------------------------------------
sub parseAirlineRef($)
{
	my($var1) = @_;
	my $ref = $var1 || undef;
	return $ref if( defined $ref and $ref =~ /^[A-Z0-9]{2}$/ );
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
	if( $at eq 'global'  or $at eq 'international'   or $at eq 'regional' or
	    $at eq 'public'  or $at eq 'gliding'         or $at eq 'airfield' or
	    $at eq 'private' or $at eq 'military/public' or $at eq 'military' )
	{
		return $at;
	}
	return 'regional';
}

#-------------------------------------------------------------------------------
sub parseLength($)
{
	my($var) = @_;
	return $var + 0 if( defined $var and $var =~ /^\d+$/ and $var >= 200 and $var <= 6000 );
	return '';
}

#-------------------------------------------------------------------------------
sub fileExport_Overpass($)
{
	my($outFile) = @_;

	my $data = decode('utf-8', OGF::Util::Overpass::runQuery_remoteRetryOptions(undef, $QUERY, 32, 'json', 3, 3));
	OGF::Util::File::writeToFile( $outFile, $data, '>:encoding(UTF-8)' ) if( defined $data );
}

#-------------------------------------------------------------------------------
sub housekeeping($$$$)
{
	my($dir, $prefix1, $prefix2, $now) = @_;
	my $KEEP_FOR = 60 * 60 * 6 ; # 6 hours
	my $dh;
	
	opendir $dh, $dir;
	while( my $file = readdir $dh )
	{
		next unless( $file =~ /^${prefix1}_\d{14}\.json/ or $file =~ /^${prefix2}_\d{14}\.json/ );
		if( $now - (stat "$dir/$file")[9] > $KEEP_FOR )
		{
			print "deleting: $dir/$file\n";
			unlink "$dir/$file";
		}
	}
	closedir $dh;
}
