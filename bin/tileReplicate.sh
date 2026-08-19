#!/bin/bash
# 
# This is derived, closely, from https://github.com/openstreetmap/chef/blob/master/cookbooks/tile/templates/default/replicate.erb
# 
# Before running updates, the replication needs to be set up with the sequence
# pyosmium-get-changes -I can be used to do this
#
# /opt/opengeofiction/OGF-terrain-tools/bin/tileReplicate.sh ogf-carto https://data.opengeofiction.net/replication/minute ogfcartogis /opt/opengeofiction/map-styles/ogf-carto/openstreetmap-carto.style /opt/opengeofiction/map-styles/ogf-carto/openstreetmap-carto.lua /var/www/html/tile/public_html/ogf-carto-replication-in-state.txt 5 19

BASE=/opt/opengeofiction/render

# parse arguments
if [ $# -lt 8 ] || [ $# -gt 9 ]; then
	cat <<USAGE
Usage:
	$0 style-name server db style-script transform-script copy-sequence-to zoom-min zoom-max [output]

	output is the osm2pgsql output, pgsql (the default) or flex. osm-carto
	v6.0.0 onwards needs flex, and then style-script is the flex lua config
	and transform-script must be none.
USAGE
	exit 1
fi
STYLE=$1
SERVER=$2
DB=$3
STYLE_SCRIPT=$4
TRANSFORM_SCRIPT=$5
COPY_SEQUENCE_TO=$6
ZOOM_MIN=$7
ZOOM_MAX=$8
OUTPUT=${9:-pgsql}

# LAG comes from the environment, not the arguments, so that a lagged style
# needs nothing but its own ogf-settings.env. Unset, or none, replicates
# normally. Otherwise it is anything GNU date understands as an offset, so
# "6 months" holds the database that far behind the API
LAG=${LAG:-none}

# Is there a style?
style_opt="--style=${STYLE_SCRIPT}"
if [ "${STYLE_SCRIPT}" = "none" ]; then
	style_opt="";
fi

# Is there a tag transform script?
transform_script_opt="--tag-transform-script=${TRANSFORM_SCRIPT}"
if [ "${TRANSFORM_SCRIPT}" = "none" ]; then
	transform_script_opt="";
fi

# The flex output takes its table and tag handling from the lua config passed
# as the style, so the pgsql-only options are dropped
output_opt=""
hstore_opt="--hstore"
multi_geometry_opt="--multi-geometry"
if [ "${OUTPUT}" = "flex" ]; then
	output_opt="--output=flex";
	hstore_opt="";
	multi_geometry_opt="";
	transform_script_opt="";
fi

# setup working dir
DIR=${BASE}/${STYLE}
cd ${DIR}

# make sure expire-queue dir is 777 so non-root user can unlink files in it
mkdir expire-queue
chmod a+rwx expire-queue

# Define exit handler
function onexit {
	[ -f sequence-prev.txt ] && mv sequence-prev.txt sequence.txt
}

# Change to the replication state directory
cd $DIR

# Install exit handler
trap onexit EXIT

# Loop indefinitely
while true
do
	echo "Loop: "$(date)
	
	# Work out the name of the next file
	file="changes-$(cat sequence.txt).osc.gz"
	efile="expiry-$(cat sequence.txt).list"
	rm -f ${file} 2> /dev/null
	rm -f ${efile} 2> /dev/null

	# Save sequence file so we can rollback if an error occurs
	cp sequence.txt sequence-prev.txt

	# A lagged style stops at a cutoff rather than catching up to now. Worked
	# out each time round, so the cutoff moves with the clock. Needs pyosmium
	# 4.1 or later for --end-date, which on Debian 13 means trixie-backports
	end_date_opt=""
	if [ "${LAG}" != "none" ]; then
		end_date_opt="--end-date=$(date -u -d "${LAG} ago" +%Y-%m-%dT%H:%M:%SZ)"
		echo "lagging by ${LAG}, ${end_date_opt}"
	fi

	# Fetch the next set of changes
	pyosmium-get-changes -vv --server=${SERVER} --sequence-file=sequence.txt --outfile=${file} --size=10 ${end_date_opt}

	# Save exit status
	status=$?

	# Check for errors
	if [ $status -eq 0 ]
	then
		# Enable exit on error
		set -e

		# Log the new data
		echo "Fetched new data from $(cat sequence-prev.txt) to $(cat sequence.txt) into ${file}"

		# Apply the changes to the database
		# (removed --flat-nodes and added --expire-tiles, --expire-output)
		osm2pgsql --database ${DB} --slim --append --number-processes=1 \
		          --expire-tiles=${ZOOM_MIN}-${ZOOM_MAX} --expire-output=${efile} \
		          ${output_opt} \
		          ${multi_geometry_opt} \
		          ${hstore_opt} \
		          ${style_opt} \
		          ${transform_script_opt} \
		          ${file}

		# No need to rollback now
		rm sequence-prev.txt

		# Get buffer count
		buffers=$(osmium fileinfo --extended --get=data.buffers.count ${file})

		# If this diff has content mark it as the latest diff
		if [ $buffers -gt 0 ]
		then
			ln -f ${file} changes-latest.osc.gz
			
			echo $(date) > ${COPY_SEQUENCE_TO}
			cat sequence.txt >> ${COPY_SEQUENCE_TO}
		fi
		
		# Queue these changes for expiry processing - note this is *not* the osc.gz as done with OSM,
		# we are still using the expiry list from osm2pgsql
		ln ${efile} expire-queue/${efile}

		# Delete old downloads & expiry lists
		find . -name 'changes-*.gz' -mmin +300 -exec rm -f {} \;
		find . -name 'expiry-*.list' -mmin +300 -exec rm -f {} \;

		# Disable exit on error
		set +e
	elif [ $status -eq 3 ]
	then
		# Log the lack of data
		echo "No new data available. Sleeping..."

		# Remove file, it will just be an empty changeset
		rm ${file}

		# Sleep for a short while
		sleep 30
	else
		# Log our failure to fetch changes
		echo "Failed to fetch changes - waiting a few minutes before retry"

		# Remove any output that was produced
		rm -f ${file}
		rm -f ${efile}

		# Wait five minutes and have another go
		sleep 300
	fi
done
