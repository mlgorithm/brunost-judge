#!/usr/bin/env bash
# Create an atomic logical backup of the standalone judge PostgreSQL service.
# The backup is local until an operator copies it to a separate failure domain.
set -euo pipefail

backup_root="${BRUNOST_JUDGE_BACKUP_DIR:-./backups}"
compose_file="${BRUNOST_JUDGE_COMPOSE_FILE:-docker-compose.yml}"
service="${BRUNOST_JUDGE_DB_SERVICE:-judge-postgres}"
retention_days="${BRUNOST_JUDGE_BACKUP_RETENTION_DAYS:-14}"

if [[ -z "$backup_root" || "$backup_root" == "/" ]]; then
  echo "Refusing unsafe backup directory: $backup_root" >&2
  exit 2
fi
mkdir -p "$backup_root"
timestamp="$(date -u +%Y-%m-%dT%H%M%SZ)"
tmp="$backup_root/.${timestamp}.tmp"
out="$backup_root/$timestamp"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp"

docker compose -f "$compose_file" exec -T "$service" pg_dump --format=custom --no-owner --no-acl -U "${POSTGRES_USER:-brunost}" -d "${POSTGRES_DB:-brunost_judge}" > "$tmp/postgres.dump"
sha256sum "$tmp/postgres.dump" > "$tmp/SHA256SUMS"
printf '%s\n' "created_at=$timestamp" "service=$service" > "$tmp/metadata.txt"
mv "$tmp" "$out"
ln -sfn "$timestamp" "$backup_root/latest"
find "$backup_root" -mindepth 1 -maxdepth 1 -type d -name '20*T*Z' -mtime "+$retention_days" -exec rm -rf {} +
echo "Backup complete: $out"
