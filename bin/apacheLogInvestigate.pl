#! /usr/bin/perl -w

#LogFormat "%h %l %u %t \"%r\" %>s %b \"%{Referer}i\" \"%{User-agent}i\" %D"
#IP logname user [datetime] "request" status bytes "referrer" "UserAgent" reqtime

#print  "reqtime\tip\tdt\treq\tstatus\tbytes\tref\tua\n";
while( <STDIN> )
{
	# match
	if( /^([0-9a-f\.\:]+) - .+ \[(.+)\] \"(.+)\" (\d{3}) (\d+|\-) \"(.+)\" \"(.+)\" (\d+)/ )
	{
		my($ip, $dt, $req, $status, $bytes, $ref, $ua, $reqtime) = ($1, $2, $3, $4, $5, $6, $7, $8);
		if( $ref eq '-' and ($status == 403) )
		{
			# and output TSV
			#print "$reqtime\t$ip\t$dt\t$req\t$status\t$bytes\t$ref\t$ua\n";
			#$useragents{$ua}++;
		}
		if( $ua =~ /(AppleWebKit\/[\d\.]+) / )
		{
			#$useragents{$1}++
		}
		if( $ua =~ /(AppleWebKit\/537\.36) / )
		{
			$useragents{$ua}++
		}
	}
	else
	{
		#print "NOMATCH $_";
	}
}

print "count\tuseragent\n";
foreach my $key ( sort keys %useragents )
{
	print "$useragents{$key}\t$key\n";
}
