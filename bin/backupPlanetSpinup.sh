#!/bin/bash
# 
# spin up a new Linode to remotely create a database backup in osm.pbf
# format, using pg_dump and planet-dump-ng

# parse arguments
if [ $# -ne 3 ]; then
	cat <<USAGE
Usage:
	$0 dir db copyto
USAGE
	exit 1
fi
BASE=$1          # /opt/opengeofiction/backup
DB=$2            # ogfdevapi
PUBLISH=$3       # /var/www/html/data.opengeofiction.net/public_html/backups
BACKUP_QUEUE=/opt/opengeofiction/backup-to-s3-queue
LOCKFILE=${BASE}/backup.lock

# constants
MINFREE=20971520 # 20GB
TIMESTAMP=$(date '+%Y%m%d_%H%M%S%Z')
LABEL="backup-$(date '+%Y%m%d%H%M%S')"
PLANET_DUMP_NG=planet-dump-ng
PLANET_DUMP_NG_THREADS=6

# linode settings
LINODECLI=~/.local/bin/linode-cli
REGION=us-west
TYPE=g6-standard-6
IMAGE=linode/ubuntu24.04
VLAN_LABEL=backup
VLAN_HOST_IP=10.98.117.1 # 98=b; 117=u
VLAN_CLIENT_IP=10.98.117.2

#### section 1: ensure dirs exist, tools installed #############################
# ensure the backups directory exists and is writable 
if [ ! -w "${BASE}/" ]; then
	echo "ERROR: ${BASE} does not exist or not writable"
	exit 2
fi

# delete old backups - older than 2 hours
echo "deleting old backups..."
find "${BASE}" -mindepth 1 -maxdepth 1 -type f -mmin +$((60*2)) -ls -delete

# ensure the publish directory exists and is writable 
if [ ! -w "${PUBLISH}/" ]; then
	echo "ERROR: ${PUBLISH} does not exist or not writable"
	exit 2
fi

# delete old published backups:
#  > monthly backups older than a year
#  > weekly backups older than a month
#  > daily backups older than a week
echo "deleting old published backups..."
find "${PUBLISH}/" -maxdepth 1 -name '*_ogf-planet-monthly.osm.pbf' -mmin +$((60*24*365)) -ls -delete
find "${PUBLISH}/" -maxdepth 1 -name '*_ogf-planet-weekly.osm.pbf' -mmin +$((60*24*30)) -ls -delete
find "${PUBLISH}/" -maxdepth 1 -name '*_ogf-planet.osm.pbf' -mmin +$((60*24*7)) -ls -delete

# files & dirs used - work out if daily, weekly, monthly or yearly
backup_pg=${TIMESTAMP}.dmp
backup_tmp=${TIMESTAMP}_ogf-planet
backup_pbf=${TIMESTAMP}_ogf-planet.osm.pbf
lastthu=$(ncal -h | awk '/Th/ {print $NF}')
today=$(date +%-d)
timeframe=daily
if [[ $(date +%u) -eq 4 ]]; then
	backup_pbf=${TIMESTAMP}_ogf-planet-weekly.osm.pbf
	timeframe=weekly
	if [[ $lastthu -eq $today ]]; then
		backup_pbf=${TIMESTAMP}_ogf-planet-monthly.osm.pbf
		timeframe=monthly
		if [[ $(date +%-m) -eq 12 ]]; then
			backup_pbf=${TIMESTAMP}_ogf-planet-yearly.osm.pbf
			timeframe=yearly
		fi
	fi
fi
latest_pbf=ogf-planet.osm.pbf

# make sure there is enough free space
cd "${BASE}"
free=`df -k --output=avail . | tail -n1`
if [[ $free -lt $MINFREE ]]; then
	echo "ERROR: Insufficient free disk space"
	exit 28
fi;

# ensure linode-cli is installed
# see https://techdocs.akamai.com/cloud-computing/docs/install-and-configure-the-cli
#  sudo apt install pipx -y
#  pipx ensurepath
#  pipx install linode-cli
if [ ! -x "${LINODECLI}" ]; then
	echo "ERROR: linode-cli is not installed at ${LINODECLI}"
	exit 3
fi

# ensure linode-cli access token is set
# see https://techdocs.akamai.com/cloud-computing/docs/manage-personal-access-tokens and https://cloud.linode.com/profile/tokens
if [[ -z "${LINODE_CLI_TOKEN}" ]]; then
	echo "ERROR: linode-cli is not configured with token in LINODE_CLI_TOKEN"
	exit 22
fi

# ensure the server is on the VLAN we're going to attach the backup
# server to, just check IP address is on an interface
if ! ip a | grep -Fq ${VLAN_HOST_IP}; then
	echo no match
	echo "ERROR: this server is not on VLAN ${VLAN_LABEL} with ip ${VLAN_HOST_IP}"
	exit 22
fi

# ensure we're not already running
if ! mkdir ${LOCKFILE} 2>/dev/null; then
	echo "backup is already running" >&2
	exit 16
else
	# release lock on clean exit, and if ...
	trap "exit" INT TERM
	trap "rm -rf ${LOCKFILE}; exit" EXIT
	echo "$0 got lock"
fi

#### section 2: setup ssh keys and cloud-init config file ######################
# generate a 128 character password, which we will not use to login
password=$(tr -dc 'A-Za-z0-9^=+' < /dev/urandom | head -c 128)

# setup a dedicated ssh key, we use this as the host key for the
# server, and for user access
#rm backup-ssh-key-*
sshkeypriv=${TIMESTAMP}-ssh
sshkeypub=${TIMESTAMP}-ssh.pub
ssh-keygen -t ed25519 -f ${sshkeypriv} -C "OGFbackup" -N '' -q
sshkey=$(cat ${sshkeypub})
sshkeyknownhost=${TIMESTAMP}-ssh-known-host

# setup cloudinit cloud-config metadata
# this checks out and builds planet-dump-ng as part of the setup
cat > ${TIMESTAMP}.yml <<EOF
#cloud-config
fqdn: ${LABEL}.opengeofiction.net
create_hostname_file: true
timezone: UTC
locale: C.UTF-8
#package_update: true
#package_upgrade: true
#package_reboot_if_required: true
packages: [git, postgresql-client, build-essential, automake, autoconf, libxml2-dev, libboost-dev, libboost-program-options-dev, libboost-date-time-dev, libboost-filesystem-dev, libboost-thread-dev, libboost-iostreams-dev, libosmpbf-dev, osmpbf-bin, libprotobuf-dev, pkg-config ]
ssh_keys:
  ed25519_private: |
$(cat ${sshkeypriv} | sed 's/^/    /')
  ed25519_public: ${sshkey}
users:
  - name: ogf
    gecos: OpenGeofiction
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    groups: [www-data, adm]
    lock_passwd: true
    ssh_authorized_keys:
      - ${sshkey}
write_files:
  - path: /opt/opengeofiction/build/build-planet-dump-ng.sh
    content: |
      #!/usr/bin/bash
      mkdir -p /opt/opengeofiction/build
      cd /opt/opengeofiction/build
      git clone https://github.com/zerebubuth/planet-dump-ng.git
      cd planet-dump-ng
      ./autogen.sh
      ./configure
      make
      make install
    permissions: '0755'
runcmd:
  # harden ssh
  - sed -i '/PermitRootLogin/d' /etc/ssh/sshd_config
  - echo "PermitRootLogin no" >> /etc/ssh/sshd_config
  - sed -i '/PasswordAuthentication/d' /etc/ssh/sshd_config
  - echo "PasswordAuthentication no" >> /etc/ssh/sshd_config
  - systemctl restart sshd
  # initialise OGF scripts
  - mkdir -p /opt/opengeofiction/tmp
  - chown -R ogf:ogf /opt/opengeofiction
  - sudo -u ogf git clone https://github.com/opengeofiction-net/OGF-terrain-tools.git /opt/opengeofiction/OGF-terrain-tools
  # build planet-dump-ng
  - /opt/opengeofiction/build/build-planet-dump-ng.sh
EOF
cloudinit=$(base64 -w0 $TIMESTAMP.yml)

#### section 3: create the cloud server ########################################
# create and provision the linode
echo "creating Linode..."
output=$(${LINODECLI} linodes create \
	--text --no-headers --no-defaults \
	--region ${REGION} \
	--type ${TYPE} \
	--image ${IMAGE} \
	--tags ogf \
	--label ${LABEL} \
	--authorized_keys "${sshkey}" \
	--root_pass "${password}" \
	--interfaces.ipam_address "" --interfaces.label "" --interfaces.purpose "public" \
	--interfaces.ipam_address "${VLAN_CLIENT_IP}/24" --interfaces.label "${VLAN_LABEL}" --interfaces.purpose "vlan" \
	--private_ip false \
	--metadata.user_data ${cloudinit} )
if [ $? -ne 0 ]; then
	echo "ERROR: unable to create linode"
	exit 38
fi
echo "creating Linode: ${output}"
read -r linode_id unused_label unused_region unused_type unused_image linode_status linode_ip unused_enc <<< ${output}
echo "created Linode ${linode_id} ${LABEL} on ${linode_ip}"

# update our trap, to ensure we always delete the linode on exit
trap "exit" INT TERM
#trap "echo deleting Linode ${linode_id}; ${LINODECLI} linodes rm ${linode_id}; rm -rf ${LOCKFILE}; exit" EXIT
trap "echo deleting Linode ${linode_id}; echo ${LINODECLI} linodes rm ${linode_id}; rm -rf ${LOCKFILE}; exit" EXIT

# save away known host key
echo "${linode_ip} ${sshkey}" > ${sshkeyknownhost}

# wait for provisioning to complete
status="status: init"
iteration=0
while [[ "$status" != "status: done" ]]; do
	((iteration++))
	if [[ $iteration -gt 10 ]]; then
		echo "ERROR: giving up provisioning after 10 waits"
		exit 121
	fi
	echo "provisioning... $(date) waiting... ($status)"
	sleep 20
	status=$(ssh -i ${sshkeypriv} -oUserKnownHostsFile=${sshkeyknownhost} ogf@${linode_ip} cloud-init status)
done
echo "provisioning... $(date) done ($status)"

#### section 4: do the backup ##################################################
# money shot: now run the backup & planet dump on the new linode
ssh -i ${sshkeypriv} -oUserKnownHostsFile=${sshkeyknownhost} ogf@${linode_ip} <<EOF
# create the postgres backup dump file
echo "backing up to ${backup_pg}"
pg_dump -h ${VLAN_HOST_IP} --format=custom --file=${backup_pg} ${DB}
if [ \$? -ne 0 ]; then
	echo "ERROR: backup failed"
else
	mkdir ${backup_tmp}
	cd ${backup_tmp}
	# run planet-dump-ng
	${PLANET_DUMP_NG} --pbf=../${backup_pbf} --dump-file=../${backup_pg} --max-concurrency=${PLANET_DUMP_NG_THREADS}
	if [ \$? -ne 0 ]; then
		echo "ERROR: planet-dump-ng failed"
	fi
	cd ..
	#rm -r ${backup_tmp}
fi
EOF

#### section 5: copy files locally and publish #################################
# copy the dmp file locally
if [ ${timeframe} != "daily" ]; then
	echo "copying ${backup_pg} locally..."
	scp -i ${sshkeypriv} -oUserKnownHostsFile=${sshkeyknownhost} ogf@${linode_ip}:${backup_pg} .
	if [ $? -eq 0 ]; then
		# queue for backup to S3 (note always weekly here)
		if [ -w "${BACKUP_QUEUE}/" ]; then
			ln ${backup_pg} ${BACKUP_QUEUE}/weekly:pgsql:${backup_pg}
		fi
	else
		echo "ERROR: failed to copy ${backup_pg} locally $?"
	fi
fi

# copy the planet file locally
echo "copying ${backup_pbf} locally..."
scp -i ${sshkeypriv} -oUserKnownHostsFile=${sshkeyknownhost} ogf@${linode_ip}:${backup_pbf} .
if [ $? -eq 0 ]; then
	# copy to the publish dir
	if [ -f "${PUBLISH}/${latest_pbf}" ]; then
		rm -f "${PUBLISH}/${latest_pbf}"
	fi
	echo "copying ${backup_pbf} to ${PUBLISH}/${backup_pbf}"
	cp ${backup_pbf} "${PUBLISH}/${backup_pbf}"
	echo "creating ${latest_pbf} link"
	ln "${PUBLISH}/${backup_pbf}" "${PUBLISH}/${latest_pbf}"

	# queue for backup to S3
	if [ -w "${BACKUP_QUEUE}/" ]; then
		ln ${backup_pbf} ${BACKUP_QUEUE}/${timeframe}:planet:${backup_pbf}
	fi
else
	echo "ERROR: failed to copy ${backup_pbf} locally $?"
fi

echo "== connect to server using:"
echo "ssh -i ${BASE}/${sshkeypriv} -oUserKnownHostsFile=${BASE}/${sshkeyknownhost} ogf@${linode_ip}"
echo "== delete server using:"
echo "linode-cli linodes rm ${linode_id}"
