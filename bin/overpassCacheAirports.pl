#! /usr/bin/perl -w

use lib '/opt/opengeofiction/OGF-terrain-tools/lib';
use strict;
use warnings;
use feature 'unicode_strings' ;
use utf8;
use Date::Format;
use Encode;
use JSON::XS;
use Math::Trig;
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
sub parseColor($);
sub fileExport_Overpass($);
sub housekeeping($$$$);
sub haversine_distance($$$$);
sub interpolate_great_circle($$$$$);
sub parseDestinationTags($);
sub validateAndBuildRoutes();
sub buildAirportRoutesSummary();
sub buildAirlineRoutes();

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
my $OUTFILE_NAME_AIRLINE_ROUTES = 'airline_routes';
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
	$OUTFILE_NAME_AIRLINE_ROUTES .= '_test';
	# query takes ~ 2s, returning ~ 0.1 MB; allow up to 20s, 2 MB
	$QUERY = << '---EOF---';
[timeout:20][maxsize:2000000][out:json];
area[type=boundary][boundary=administrative][admin_level=2]["ogf:id"~"^(BG01|AR120|UL04k|UL05[ab])$"]->.territories;
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
my %airportData;  # stores full airport data by code for route processing
my %rawRoutes;    # stores raw route data: $rawRoutes{$airportCode}{$airlineCode} = [@destinationCodes]
my @airlineRoutesOut;
my @airlineRoutesErrors;
my $records = $results->{elements};
my %currentTerritory;
my %seenTerritories;
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
			next;
		}
		my $ogfId = $record->{'tags'}{'ogf:id'};
		
		# check for duplicate ogf:id
		if( exists $seenTerritories{$ogfId} )
		{
			print "> DUPLICATE ogf:id $ogfId: $seenTerritories{$ogfId} vs $id\n"
		}
		$seenTerritories{$ogfId} = $id;
		
		# is the territory canonical?
		if( exists $canonicalTerritories{$ogfId} and $ogfId ne $record->{tags}->{'name'} )
		{
			$currentTerritory{'ogf:id'}             = $ogfId;
			$currentTerritory{'is_in:country'}      = $record->{'tags'}{'int_name'} || $record->{'tags'}{'name'} || $ogfId;
			$currentTerritory{'is_in:country:wiki'} = $record->{'tags'}{'ogf:wiki'} || $record->{'tags'}{'ogfwiki'} || $currentTerritory{'is_in:country'};
			$currentTerritory{'is_in:continent'}    = parseContinent $record->{'tags'}{'is_in:continent'}, $ogfId;
	
			print "> parsing airports in $canonicalTerritories{$ogfId} $ogfId: $record->{tags}->{name}\n";
		}
		elsif( exists $canonicalTerritories{$ogfId} and $ogfId eq $record->{tags}->{'name'} )
		{
			print "> SKIPPING airports in $ogfId: territory name not set\n";
		}
		else
		{
			print "> SKIPPING airports and airlines in non-canonical $ogfId: $record->{tags}->{name}\n";
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
		$entry->{'ogf:logo'}           = $record->{tags}->{'ogf:logo'} || '';
		$entry->{'ogf:permission'}     = parsePermission $record->{tags}->{'ogf:permission'};
		$entry->{'type'}               = parseAerodromeType $record->{tags}->{'aerodrome:type'};
		$entry->{'runway'}             = '';
		$entry->{'runways'}            = ();
		$entry->{'runways:count'}      = 0;
		$entry->{'gates:count'}        = 0;
		$entry->{'terminals:count'}    = 0;
		$entry->{'terminals'}          = ();
		$entry->{'destinations'}       = parseDestinationTags $record->{tags};
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
	elsif( exists $record->{tags}->{'economy:iclass'} and $record->{tags}->{'economy:iclass'} =~ /[Aa]irline/ and
	       exists $currentTerritory{'ogf:id'} )
	{
		my $airline = {};
		$airline->{'ref'}                = parseAirlineRef $record->{tags}->{ref};
		$airline->{'name'}               = $record->{tags}->{brand} || $record->{tags}->{name};
		$airline->{'description'}        = parseStr $record->{tags}->{description}, undef, '', 100;
		$airline->{'ogf:id'}             = $currentTerritory{'ogf:id'};
		$airline->{'is_in:continent'}    = $currentTerritory{'is_in:continent'};
		$airline->{'is_in:country'}      = $currentTerritory{'is_in:country'};
		$airline->{'is_in:country:wiki'} = $currentTerritory{'is_in:country:wiki'};
		$airline->{'is_in:city'}         = $record->{tags}->{'is_in:city'} || 'unknown';
		$airline->{'id'}                 = $id;
		$airline->{'lat'}                = $record->{lat} || $record->{center}->{lat};
		$airline->{'lon'}                = $record->{lon} || $record->{center}->{lon};
		$airline->{'ogf:logo'}           = $record->{tags}->{'ogf:logo'} || '';
		$airline->{'ogf:permission'}     = parsePermission $record->{tags}->{'ogf:permission'};
		$airline->{'color'}              = parseColor $record->{tags}->{'color'};
		addAirline $airline;
	}
	else
	{
	}
}

# current airport entry to flush out?
addAirport $entry; $entry = {};

# process routes - validate reciprocal tagging and build route structures
print "validating routes and building route data...\n";
validateAndBuildRoutes();
buildAirportRoutesSummary();
buildAirlineRoutes();

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
$publishFile = $PUBLISH_DIR . '/' . $OUTFILE_NAME_AIRLINE_ROUTES . '.json';
$json = JSON::XS->new->canonical->indent(2)->space_after;
$text = $json->encode( \@airlineRoutesOut );
OGF::Util::File::writeToFile($publishFile, $text, '>:encoding(UTF-8)' );
$publishFile = $PUBLISH_DIR . '/' . $OUTFILE_NAME_AIRLINE_ROUTES . '_errors.json';
$json = JSON::XS->new->canonical->indent(2)->space_after;
$text = $json->encode( \@airlineRoutesErrors );
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

		# Store airport data for route processing
		$airportData{$entry->{'ref'}} = $entry;

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
sub parseColor($)
{
	my($var) = @_;

	# Check if valid hex color format (#RRGGBB)
	if( defined $var and $var =~ /^#[0-9A-Fa-f]{6}$/ )
	{
		return uc $var;  # Return uppercase hex color
	}

	# Generate random vibrant color for map visualization
	# Use HSL color space for better visual distribution
	my $hue = int(rand(360));  # Random hue 0-359
	my $saturation = 70 + int(rand(30));  # Saturation 70-100%
	my $lightness = 45 + int(rand(15));   # Lightness 45-60% (avoid too dark/light)

	# Convert HSL to RGB
	my $c = (1 - abs(2 * $lightness / 100 - 1)) * $saturation / 100;
	my $x = $c * (1 - abs(($hue / 60) % 2 - 1));
	my $m = $lightness / 100 - $c / 2;

	my ($r, $g, $b);
	if ($hue < 60) {
		($r, $g, $b) = ($c, $x, 0);
	} elsif ($hue < 120) {
		($r, $g, $b) = ($x, $c, 0);
	} elsif ($hue < 180) {
		($r, $g, $b) = (0, $c, $x);
	} elsif ($hue < 240) {
		($r, $g, $b) = (0, $x, $c);
	} elsif ($hue < 300) {
		($r, $g, $b) = ($x, 0, $c);
	} else {
		($r, $g, $b) = ($c, 0, $x);
	}

	# Convert to 0-255 range and format as hex
	$r = int(($r + $m) * 255);
	$g = int(($g + $m) * 255);
	$b = int(($b + $m) * 255);

	return sprintf("#%02X%02X%02X", $r, $g, $b);
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

#-------------------------------------------------------------------------------
# great circle calculation functions (adapted from greatcircle.pl)
#-------------------------------------------------------------------------------
sub haversine_distance($$$$)
{
	my ($lat1, $lon1, $lat2, $lon2) = @_;
	my $R = 6371; # Earth radius in km

	my $dlat = deg2rad($lat2 - $lat1);
	my $dlon = deg2rad($lon2 - $lon1);

	$lat1 = deg2rad($lat1);
	$lat2 = deg2rad($lat2);

	my $a = sin($dlat/2)**2 + cos($lat1) * cos($lat2) * sin($dlon/2)**2;
	my $c = 2 * atan2(sqrt($a), sqrt(1 - $a));

	return $R * $c;
}

#-------------------------------------------------------------------------------
sub interpolate_great_circle($$$$$)
{
	my ($lat1, $lon1, $lat2, $lon2, $interval_km) = @_;

	my $distance = haversine_distance($lat1, $lon1, $lat2, $lon2);
	return () if $distance == 0;

	my $num_points = int($distance / $interval_km);
	$num_points = 5 if $num_points < 5;  # minimum 5 points

	my @points;
	$lat1 = deg2rad($lat1);
	$lon1 = deg2rad($lon1);
	$lat2 = deg2rad($lat2);
	$lon2 = deg2rad($lon2);

	my $d_rad = $distance / 6371;
	my $sin_d = sin($d_rad);

	for my $i (0 .. $num_points) {
		my $f = $i / $num_points;

		my $A = sin((1 - $f) * $d_rad) / $sin_d;
		my $B = sin($f * $d_rad) / $sin_d;

		my $x = $A * cos($lat1) * cos($lon1) + $B * cos($lat2) * cos($lon2);
		my $y = $A * cos($lat1) * sin($lon1) + $B * cos($lat2) * sin($lon2);
		my $z = $A * sin($lat1) + $B * sin($lat2);

		my $lat = atan2($z, sqrt($x**2 + $y**2));
		my $lon = atan2($y, $x);

		push @points, [rad2deg($lon), rad2deg($lat)];  # [lon, lat] for GeoJSON
	}

	return @points;
}

#-------------------------------------------------------------------------------
# parse destination:XX tags from airport tags
#-------------------------------------------------------------------------------
sub parseDestinationTags($)
{
	my($tags) = @_;
	my %destinations;

	return \%destinations if !defined $tags;

	# find all destination:XX tags
	foreach my $key (keys %$tags)
	{
		if( $key =~ /^destination:([A-Z0-9]{2})$/ )
		{
			my $airlineCode = uc $1;
			my $destList = $tags->{$key};

			# split by semicolon and clean up
			my @dests = split /;/, $destList;
			@dests = map { uc $_ } @dests;  # uppercase
			@dests = map { s/^\s+|\s+$//gr } @dests;  # trim whitespace
			@dests = grep { /^[A-Z]{3}$/ } @dests;  # only valid 3-letter codes

			$destinations{$airlineCode} = \@dests if @dests;
		}
	}

	return \%destinations;
}

#-------------------------------------------------------------------------------
# validate routes and check for reciprocal tagging
#-------------------------------------------------------------------------------
sub validateAndBuildRoutes()
{
	print "> validating reciprocal routes...\n";

	# build validated routes hash
	my %validatedRoutes;  # $validatedRoutes{$airlineCode}{$originCode}{$destCode} = 1

	# for each airport, check its destinations
	foreach my $originCode (keys %airportData)
	{
		my $airport = $airportData{$originCode};
		my $destinations = $airport->{'destinations'};

		foreach my $airlineCode (keys %$destinations)
		{
			foreach my $destCode (@{$destinations->{$airlineCode}})
			{
				# check if destination airport exists
				if (!exists $airportData{$destCode})
				{
					push @airlineRoutesErrors, {
						'origin' => $originCode,
						'destination' => $destCode,
						'airline' => $airlineCode,
						'text' => "destination airport $destCode not found in canonical airports"
					};
					next;
				}

				# check if destination has reciprocal tag
				my $destAirport = $airportData{$destCode};
				my $destDestinations = $destAirport->{'destinations'};

				if (!exists $destDestinations->{$airlineCode})
				{
					push @airlineRoutesErrors, {
						'origin' => $originCode,
						'destination' => $destCode,
						'airline' => $airlineCode,
						'text' => "missing reciprocal: $destCode does not list $airlineCode routes"
					};
					next;
				}

				# check if destination lists origin in its routes
				my @destRoutes = @{$destDestinations->{$airlineCode}};
				if (!grep { $_ eq $originCode } @destRoutes)
				{
					push @airlineRoutesErrors, {
						'origin' => $originCode,
						'destination' => $destCode,
						'airline' => $airlineCode,
						'text' => "missing reciprocal: $destCode does not list $originCode in destination:$airlineCode"
					};
					next;
				}

				# valid reciprocal route found
				$validatedRoutes{$airlineCode}{$originCode}{$destCode} = 1;
				print "  valid route: $airlineCode $originCode -> $destCode\n";
			}
		}
	}

	# store validated routes in global hash for other functions
	%rawRoutes = %validatedRoutes;
}

#-------------------------------------------------------------------------------
# build simple airport routes summary - just list of destination codes
#-------------------------------------------------------------------------------
sub buildAirportRoutesSummary()
{
	print "> building airport routes summary...\n";

	# for each airport in output, add simple list of validated destinations
	foreach my $airport (@airportOut)
	{
		my $airportCode = $airport->{'ref'};
		my %allDestinations;

		# collect all unique destination codes from all airlines
		foreach my $airlineCode (keys %rawRoutes)
		{
			next if !exists $rawRoutes{$airlineCode}{$airportCode};

			foreach my $destCode (keys %{$rawRoutes{$airlineCode}{$airportCode}})
			{
				# skip self-routes
				next if $destCode eq $airportCode;

				$allDestinations{$destCode} = 1;
			}
		}

		# convert to sorted array of airport codes
		my @destinationList = sort keys %allDestinations;

		# add to airport record - only if there are routes
		if (@destinationList)
		{
			$airport->{'destinations'} = \@destinationList;
		}
	}
}

#-------------------------------------------------------------------------------
# build airline routes JSON with great circle geometry
# outputs a FLAT array of route objects for easy MediaWiki consumption
#-------------------------------------------------------------------------------
sub buildAirlineRoutes()
{
	print "> building airline routes with geometry...\n";

	# build flat array of route objects
	foreach my $airlineCode (keys %rawRoutes)
	{
		# find airline details
		my $airlineName = $airlineCode;
		my $airlineOgfId = '';
		my $airlineCountry = '';
		my $airlineColor = '#0066CC';  # default blue color
		foreach my $airline (@airlineOut)
		{
			if ($airline->{'ref'} eq $airlineCode)
			{
				$airlineName = $airline->{'name'};
				$airlineOgfId = $airline->{'ogf:id'};
				$airlineCountry = $airline->{'is_in:country'};
				$airlineColor = $airline->{'color'} if defined $airline->{'color'};
				last;
			}
		}

		# get all origin airports for this airline
		foreach my $originCode (keys %{$rawRoutes{$airlineCode}})
		{
			my $originAirport = $airportData{$originCode};

			# get all destinations from this origin
			foreach my $destCode (keys %{$rawRoutes{$airlineCode}{$originCode}})
			{
				# skip routes where origin and destination are the same
				next if $originCode eq $destCode;

				my $destAirport = $airportData{$destCode};

				# calculate great circle distance
				my $distance = haversine_distance(
					$originAirport->{'lat'}, $originAirport->{'lon'},
					$destAirport->{'lat'}, $destAirport->{'lon'}
				);

				# generate great circle geometry (point every ~500km)
				my @geometry = interpolate_great_circle(
					$originAirport->{'lat'}, $originAirport->{'lon'},
					$destAirport->{'lat'}, $destAirport->{'lon'},
					500
				);

				# convert to Leaflet polyline format: [[lat, lon], [lat, lon], ...]
				# @geometry contains [lon, lat] pairs, reverse to [lat, lon] for Leaflet
				my @polyline = map { [$_->[1], $_->[0]] } @geometry;

				# build flat route object with all data at top level
				push @airlineRoutesOut, {
					'airline_code' => $airlineCode,
					'airline_name' => $airlineName,
					'airline_ogf:id' => $airlineOgfId,
					'airline_country' => $airlineCountry,
					'color' => $airlineColor,
					'origin_code' => $originCode,
					'origin_name' => $originAirport->{'name'},
					'origin_city' => $originAirport->{'serves'},
					'origin_country' => $originAirport->{'is_in:country'},
					'origin_ogf:id' => $originAirport->{'ogf:id'},
					'origin_lat' => $originAirport->{'lat'},
					'origin_lon' => $originAirport->{'lon'},
					'dest_code' => $destCode,
					'dest_name' => $destAirport->{'name'},
					'dest_city' => $destAirport->{'serves'},
					'dest_country' => $destAirport->{'is_in:country'},
					'dest_ogf:id' => $destAirport->{'ogf:id'},
					'dest_lat' => $destAirport->{'lat'},
					'dest_lon' => $destAirport->{'lon'},
					'distance_km' => int($distance + 0.5),
					'polyline' => \@polyline,
					'text' => "<b>$airlineName</b> ($airlineCode)<br/>$originAirport->{'name'} ($originAirport->{'serves'}, $originAirport->{'is_in:country'})<br/>→<br/>$destAirport->{'name'} ($destAirport->{'serves'}, $destAirport->{'is_in:country'})<br/><i>Distance: " . int($distance + 0.5) . " km</i>"
				};
			}
		}
	}

	# sort routes by airline, then origin, then destination
	@airlineRoutesOut = sort {
		$a->{'airline_code'} cmp $b->{'airline_code'} ||
		$a->{'origin_code'} cmp $b->{'origin_code'} ||
		$a->{'dest_code'} cmp $b->{'dest_code'}
	} @airlineRoutesOut;
}
