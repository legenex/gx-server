#!/usr/bin/env bash
# Consistent dumps of live databases into a staging directory. Never prints secrets.
set -euo pipefail
STAMP="${GX_DUMP_STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
OUT="${1:-$HOME/Documents/Backups/dumps/$STAMP}"
mkdir -p "$OUT"
chmod 700 "$OUT"

dump_pg() {
  local container="$1" dest="$2"
  if docker ps --format '{{.Names}}' | grep -qx "$container"; then
    echo "dump postgres $container"
    docker exec "$container" sh -c 'pg_dumpall -U "${POSTGRES_USER:-postgres}"' >"$dest" 2>/dev/null \
      || docker exec "$container" pg_dumpall -U postgres >"$dest" 2>/dev/null \
      || docker exec "$container" pg_dumpall >"$dest"
    gzip -f "$dest"
  else
    echo "skip missing container $container"
  fi
}

dump_pg gx-litellm-db "$OUT/litellm.sql"
dump_pg pageflo-gx10-preview-postgres-1 "$OUT/pageflo.sql"
dump_pg financialos-db-1 "$OUT/financialos.sql"

if docker ps --format '{{.Names}}' | grep -qx open-webui; then
  echo "dump open-webui volume"
  docker run --rm -v open-webui:/data:ro -v "$OUT":/out alpine \
    tar -C /data -czf /out/open-webui-data.tar.gz .
fi

# SQLite trees that applications keep outside Docker
find /srv/projects /home/legenex/Documents /home/legenex/agentos \
  -name '*.db' -o -name '*.sqlite' -o -name '*.sqlite3' 2>/dev/null \
  | grep -v node_modules | grep -v '.git/' | head -200 \
  | while read -r db; do
      rel=$(echo "$db" | sed 's#^/##; s#/#_#g')
      sqlite3 "$db" ".backup '$OUT/sqlite-$rel'" 2>/dev/null || cp -a "$db" "$OUT/sqlite-$rel.copy"
    done

echo "$OUT" >"$OUT/DUMP-PATH.txt"
echo "dumps written to $OUT"
