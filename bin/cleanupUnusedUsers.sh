#!/bin/bash
# Remove unused accounts from the API database - see etc/cleanupUnusedUsers.sql
# for what qualifies. Monthly from ogfutil-cleanupUnusedUsers.timer, as the
# ogf user, which connects as a PostgreSQL superuser.
#
# Usage: cleanupUnusedUsers.sh [--dry-run] [database]
set -euo pipefail

ACTION=COMMIT
if [ "${1:-}" = "--dry-run" ]; then ACTION=ROLLBACK; shift; fi
DB=${1:-${DB_NAME:-ogfdevapi}}
SQL=$(dirname "$0")/../etc/cleanupUnusedUsers.sql

psql -v ON_ERROR_STOP=1 -v action=${ACTION} -f "${SQL}" "${DB}"
