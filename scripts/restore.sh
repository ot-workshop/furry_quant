#!/bin/sh
set -eu

test $# = 1 || { echo 'Usage: scripts/restore.sh BACKUP_DIRECTORY' >&2; exit 2; }
backup_dir=$(cd "$1" && pwd)
cd "$(dirname "$0")/.."
set -a
. ./.env
set +a
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
storage_root=${STORAGE_ROOT:-./storage}

(cd "$backup_dir" && sha256sum -c SHA256SUMS)
if tar -tf "$backup_dir/artifacts.tar" | awk 'BEGIN{bad=0} /^\// || /(^|\/)\.\.($|\/)/ || /^-/{bad=1} END{exit bad}'; then :; else
  echo 'Backup contains an unsafe artifact path.' >&2; exit 2
fi
rows=$(docker compose --env-file .env -f docker-compose.yaml exec -T db psql -U "${POSTGRES_USER:-quant}" -d "${POSTGRES_DB:-finance_quant}" -Atc \
  "select (select count(*) from research_run)+(select count(*) from research_task)+(select count(*) from data_release);" | tr -d '[:space:]')
test "$rows" = 0 || { echo 'Restore target is not a fresh project database.' >&2; exit 2; }
while IFS= read -r relative; do
  test -n "$relative" || continue
  test ! -e "$storage_root/$relative" || { echo "Restore target already exists: $relative" >&2; exit 2; }
done < "$backup_dir/artifact-list.txt"

docker compose --env-file .env -f docker-compose.yaml stop api worker
docker compose --env-file .env -f docker-compose.yaml exec -T db pg_restore -U "${POSTGRES_USER:-quant}" -d "${POSTGRES_DB:-finance_quant}" \
  --clean --if-exists --no-owner < "$backup_dir/database.dump"
tar -C "$storage_root" -xf "$backup_dir/artifacts.tar"
docker compose --env-file .env -f docker-compose.yaml up -d api worker
docker compose --env-file .env -f docker-compose.yaml exec -T api python -m finance_quant.server check
