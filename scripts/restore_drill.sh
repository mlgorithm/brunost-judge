#!/usr/bin/env bash
# Restore the latest logical backup into a throwaway PostgreSQL container.
# This never writes to the live database.
set -euo pipefail

backup_root="${BRUNOST_JUDGE_BACKUP_DIR:-./backups}"
dump="${BRUNOST_JUDGE_RESTORE_DUMP:-$backup_root/latest/postgres.dump}"
scratch="brunost-judge-restore-drill-$$"
image="${BRUNOST_JUDGE_POSTGRES_IMAGE:-postgres:16-alpine}"
user="${POSTGRES_USER:-brunost}"
db="${POSTGRES_DB:-brunost_judge}"
trap 'docker rm -f "$scratch" >/dev/null 2>&1 || true' EXIT

[[ -f "$dump" ]] || { echo "Backup not found: $dump" >&2; exit 2; }
if [[ -f "${dump%/*}/SHA256SUMS" ]]; then
  (cd "${dump%/*}" && sha256sum -c SHA256SUMS)
fi
docker run -d --name "$scratch" -e POSTGRES_USER="$user" -e POSTGRES_PASSWORD=restore-drill -e POSTGRES_DB="$db" "$image" >/dev/null
for _ in $(seq 1 60); do
  docker exec "$scratch" pg_isready -U "$user" -d "$db" >/dev/null 2>&1 && break
  sleep 1
done
docker exec -i "$scratch" pg_restore --no-owner --no-acl --exit-on-error -U "$user" -d "$db" < "$dump"
tables="$(docker exec "$scratch" psql -U "$user" -d "$db" -Atc "select count(*) from information_schema.tables where table_schema='public';")"
[[ "${tables:-0}" -gt 0 ]] || { echo "Restore produced no public tables" >&2; exit 1; }
echo "Restore drill passed: $tables public tables"
