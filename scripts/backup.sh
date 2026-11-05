#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
set -a
. ./.env
set +a
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"

storage_root=${STORAGE_ROOT:-./storage}
backup_root=${BACKUP_ROOT:-./backups}
active=$(docker compose --env-file .env -f docker-compose.yaml exec -T db psql -U "${POSTGRES_USER:-quant}" -d "${POSTGRES_DB:-finance_quant}" -Atc \
  "select count(*) from research_task where status in ('queued','running');" | tr -d '[:space:]')
test "$active" = 0 || { echo "Refusing backup while $active task(s) are queued or running." >&2; exit 2; }

stamp=$(date -u +%Y%m%dT%H%M%SZ)
destination="$backup_root/$stamp"
mkdir -p "$destination"

docker compose --env-file .env -f docker-compose.yaml exec -T db pg_dump -U "${POSTGRES_USER:-quant}" -d "${POSTGRES_DB:-finance_quant}" \
  --format=custom --no-owner > "$destination/database.dump"
docker compose --env-file .env -f docker-compose.yaml exec -T db psql -U "${POSTGRES_USER:-quant}" -d "${POSTGRES_DB:-finance_quant}" -Atc \
  "select relative_path from run_artifact union select relative_path from dataset where status='ready' order by 1;" \
  > "$destination/artifact-list.txt"

if awk 'BEGIN{bad=0} /^\// || /(^|\/)\.\.($|\/)/ || /^-/{bad=1} END{exit bad}' "$destination/artifact-list.txt"; then :; else
  echo 'Database contains an unsafe artifact path.' >&2; exit 2
fi
if test -s "$destination/artifact-list.txt"; then
  tar --verbatim-files-from --no-unquote -C "$storage_root" -cf "$destination/artifacts.tar" -T "$destination/artifact-list.txt"
else
  tar -cf "$destination/artifacts.tar" --files-from /dev/null
fi
docker compose --env-file .env -f docker-compose.yaml exec -T db psql -U "${POSTGRES_USER:-quant}" -d "${POSTGRES_DB:-finance_quant}" -Atc \
  "select version_num from alembic_version;" > "$destination/schema-version.txt"
(cd "$destination" && sha256sum database.dump artifacts.tar artifact-list.txt schema-version.txt > SHA256SUMS)
echo "$destination"
