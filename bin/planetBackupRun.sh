#!/bin/bash
# Run the planet backup the way this server is configured to: locally
# (backupPlanet.sh) or on an on-demand Linode (backupPlanetSpinup.sh), chosen
# by PLANET_BACKUP_MODE from /etc/opengeofiction/planet-backup.env, which
# planet-backup.service reads and which also carries LINODE_CLI_TOKEN for the
# spin-up. Replaces the systemd override the API server used to need.
#
# Usage: planetBackupRun.sh <backup dir> <database> <publish dir>
set -euo pipefail

BIN=$(dirname "$(readlink -f "$0")")
case "${PLANET_BACKUP_MODE:-local}" in
	local)  exec "${BIN}/backupPlanet.sh" "$@" ;;
	spinup) exec "${BIN}/backupPlanetSpinup.sh" "$@" ;;
	*) echo "planetBackupRun.sh: PLANET_BACKUP_MODE must be local or spinup, not '${PLANET_BACKUP_MODE}'" >&2; exit 2 ;;
esac
