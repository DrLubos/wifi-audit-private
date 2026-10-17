#!/bin/sh
# Ingest tokens of the sensors (docs/live-upload.md 6.1), as the database owner
# on the box (the developer runs it - Claude never creates or sees a token):
#
#   cd ~/wifi-audit/server
#   sh ops/sensor_token.sh create <sensor> [label]   # prints the token ONCE
#   sh ops/sensor_token.sh create <sensor> [label] --stdout   # only the token on stdout, for a pipe:
#     ssh google 'cd ~/wifi-audit/server && sh ops/sensor_token.sh create pi-fri live --stdout' \
#       | ssh pi 'umask 077 && cat > ~/.wifi-sensor-ingest-token'
#     (then install_uploader.sh moves it to /etc/wifi-sensor/ingest.token; the
#     token is never shown - if it ever is, revoke it and make a new one)
#   sh ops/sensor_token.sh list
#   sh ops/sensor_token.sh revoke <token id>
#
# A token is wsi1.<id>.<64 hex>. The database stores only its SHA-256: the
# hash is computed here in the shell (sha256sum), so the plaintext token
# never reaches PostgreSQL, a log or a process argument (the secret goes from
# openssl's stdout to this shell's variable and to your terminal, once). Put
# it into the password manager and into the Pi's /etc/wifi-sensor/uploader.conf
# (INGEST_TOKEN=...). Several active tokens per sensor are allowed (rotate:
# create, switch the Pi, revoke the old one). Deleting a sensor in Settings
# deletes its tokens.
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "sensor_token: no .env next to docker-compose.yml" >&2; exit 1; }
set -a; . ./.env; set +a
psql_db() { docker compose exec -T db psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 "$@"; }

usage() { echo "usage: sh ops/sensor_token.sh create <sensor> [label] [--stdout] | list | revoke <token id>" >&2; exit 2; }

case "${1:-}" in
  create)
    [ $# -ge 2 ] || usage
    sensor=$2; label=""; to_stdout=0
    for a in "${3:-}" "${4:-}"; do
      case "$a" in --stdout) to_stdout=1 ;; "") ;; *) label=$a ;; esac
    done
    exists=$(psql_db -At -v sensor="$sensor" <<'SQL'
SELECT count(*) FROM sensors WHERE name = :'sensor';
SQL
)
    [ "$exists" = 1 ] || { echo "sensor_token: no sensor named '$sensor'" >&2; exit 1; }
    secret=$(openssl rand -hex 32)
    id=$(echo "SELECT nextval('sensor_tokens_id_seq')" | psql_db -At)
    token="wsi1.$id.$secret"
    hash=$(printf %s "$token" | sha256sum | cut -d' ' -f1)
    psql_db -v id="$id" -v sensor="$sensor" -v hash="$hash" -v label="$label" <<'SQL'
INSERT INTO sensor_tokens (id, sensor_id, token_hash, label)
SELECT :id, s.id, decode(:'hash', 'hex'), nullif(:'label', '') FROM sensors s WHERE s.name = :'sensor';
SQL
    if [ "$to_stdout" = 1 ]; then
      echo "token $id for sensor $sensor created (written to stdout only)" >&2
      printf '%s\n' "$token"
    else
      echo "token $id for sensor $sensor created. Shown once - store it now:"
      echo
      echo "  $token"
      echo
      echo "On the Pi: in /etc/wifi-sensor/ingest.token (root:<sensor user> 0640)"
    fi
    ;;
  list)
    psql_db <<'SQL'
\pset pager off
SELECT t.id, s.name AS sensor, t.label, t.created_at, t.last_used_at, t.revoked_at
FROM sensor_tokens t JOIN sensors s ON s.id = t.sensor_id ORDER BY t.id;
SQL
    ;;
  revoke)
    [ $# -eq 2 ] || usage
    case "$2" in *[!0-9]*|'') usage ;; esac
    psql_db -v id="$2" <<'SQL'
\pset pager off
UPDATE sensor_tokens SET revoked_at = now() WHERE id = :id AND revoked_at IS NULL
RETURNING id, sensor_id, revoked_at;
SQL
    ;;
  *) usage ;;
esac
