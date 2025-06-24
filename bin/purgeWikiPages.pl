#! /usr/bin/perl -w

use lib '/opt/opengeofiction/OGF-terrain-tools/lib';
use strict;
use warnings;
use feature 'unicode_strings' ;
use utf8;
use URI::Escape;
use LWP::UserAgent;
use JSON::XS;
use OGF::Util::Usage qw( usageInit usageError );

# parse arguments
my %opt;
usageInit( \%opt, qq/ h ds=s pp=s/, << "*" );
[-ds <dataset>] [-pp <wiki php page purge script>]

-ds     Which schedule to purge
-pp     MediaWiki PHP page purge script
*
usageError() if $opt{'h'};
my $URL_BASE = 'https://wiki.opengeofiction.net/api.php?action=query&format=json&list=categorymembers&cmlimit=100&cmtitle=';
my $query = $URL_BASE . uri_escape('Category:Automated pages/' . ($opt{'ds'} ? $opt{'ds'} : 'schedule1'));
my $WIKI_PURGE = ($opt{'pp'} ? $opt{'pp'} : 'php /var/www/html/wiki.opengeofiction.net/public_html/maintenance/purgePage.php');

# get data
print "query: $query\n";
my $userAgent = LWP::UserAgent->new(keep_alive => 20, agent => 'OGF-purgeWikiPages.pl/2025.06');
my $resp = $userAgent->get($query);
die qq/Cannot read $query/ unless( $resp->is_success );
my $results = JSON::XS->new->utf8->decode ($resp->content);

# start PHP page purge script
# note: the script could have used mediawiki.org/wiki/API:Purge POST API call,
# which would mean it could run on the util server, rather than wiki server
open my $fh => "| $WIKI_PURGE " or die $!;

# for each page in the category
my $pages = $results->{query}->{categorymembers};
for my $page ( @$pages )
{
	print "> $page->{title}\n";
	print $fh "$page->{title}\n";
}

close $fh or die $!;
print "complete\n";
