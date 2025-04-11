#!/bin/bash
# 
# spin up a new Linode 

# parse arguments
if [ $# -ne 3 ]; then
	cat <<USAGE
Usage:
	$0 labelbase type image
	e.g.
		$0 ogftest g6-nanode-1 ubuntu24.04
USAGE
	exit 1
fi
BASE=/opt/opengeofiction/tmp
TIMESTAMP=$(date '+%Y%m%d%H%M')
LABEL="$1-${TIMESTAMP}"
REGION=us-west
TYPE=$2
IMAGE="linode/$3"
VLAN_LABEL=backup
VLAN_HOST_IP=10.98.117.1 # 98=b; 117=u
VLAN_CLIENT_IP="10.98.117.$(($RANDOM % 250 + 4))"

#### section 1: ensure dirs exist, tools installed #############################
# ensure the base directory exists and is writable 
if [ ! -w "${BASE}/" ]; then
	echo "ERROR: ${BASE} does not exist or not writable"
	exit 2
fi
cd "${BASE}"

# ensure linode-cli is installed
# see https://techdocs.akamai.com/cloud-computing/docs/install-and-configure-the-cli
#  sudo apt install pipx -y
#  pipx ensurepath
#  pipx install linode-cli
LINODECLI=~/.local/bin/linode-cli
if [ ! -x "${LINODECLI}" ]; then
	echo "ERROR: linode-cli is not installed at ${LINODECLI}"
	exit 3
fi
LINODECLI="${LINODECLI} --no-defaults --suppress-warnings"

# ensure linode-cli access token is set
# see https://techdocs.akamai.com/cloud-computing/docs/manage-personal-access-tokens and https://cloud.linode.com/profile/tokens
if [[ -z "${LINODE_CLI_TOKEN}" ]]; then
	echo "ERROR: linode-cli is not configured with token in LINODE_CLI_TOKEN"
	exit 22
fi

# ensure the server is on the VLAN we're going to attach the
# server to, just check IP address is on an interface
if ! ip a | grep -Fq ${VLAN_HOST_IP}; then
	echo no match
	echo "ERROR: this server is not on VLAN ${VLAN_LABEL} with ip ${VLAN_HOST_IP}"
	exit 22
fi

#### section 2: setup ssh keys and cloud-init config file ######################
# generate a 128 character password, which we will not use to login
password=$(head -c 128 <(tr -dc 'A-Za-z0-9^=+' < /dev/urandom 2>/dev/null))

# setup a dedicated ssh key, we use this as the host key for the
# server, and for user access
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
packages: [git, postgresql-client, osmpbf-bin ]
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
EOF
cloudinit=$(base64 -w0 $TIMESTAMP.yml)

#### section 3: create the cloud server ########################################
# create and provision the linode
echo "creating Linode..."
output=$(${LINODECLI} linodes create \
	--text --no-headers \
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
echo "created Linode ${linode_id} ${LABEL} on ${linode_ip} / ${VLAN_CLIENT_IP}"

# save away known host key
echo "${linode_ip} ${sshkey}" > ${sshkeyknownhost}
echo "${VLAN_CLIENT_IP} ${sshkey}" >> ${sshkeyknownhost}

# wait for provisioning to complete
status="status: init"
iteration=0
while [[ "$status" != "status: done" ]]; do
	((iteration++))
	if [[ $iteration -gt 25 ]]; then
		echo "ERROR: giving up provisioning after 25 waits"
		exit 121
	fi
	echo "provisioning... $(date) waiting... ($status)"
	sleep 20
	status=$(ssh -i ${sshkeypriv} -oUserKnownHostsFile=${sshkeyknownhost} ogf@${VLAN_CLIENT_IP} cloud-init status)
done
echo "provisioning... $(date) done ($status)"

echo "== connect to server using:"
echo "ssh -i ${sshkeypriv} -oUserKnownHostsFile=${sshkeyknownhost} ogf@${VLAN_CLIENT_IP}"

