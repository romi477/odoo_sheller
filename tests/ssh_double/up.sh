#!/bin/sh
# Builds and starts the ssh double for tests/test_e2e_ssh.py, and stops it with
# `docker rm -f odoo-sheller-ssh-double`. It listens on 127.0.0.1 only.
#
# Everything this machine-specific has a default for the author's setup and an
# environment variable for anybody else's:
#   PT_E2E_SSH_BASE      an image with Odoo's Python dependencies installed
#   PT_E2E_SSH_DB_FROM   a running container whose DB_HOST/DB_USER/DB_PASSWORD
#                        the double borrows (the password never touches a file
#                        this script leaves behind)
#   PT_E2E_SSH_NETWORK   the Docker network that reaches that database
#   PT_E2E_SSH_ODOO_SRC  the Odoo source, mounted read-only at /opt/odoo
#   PT_E2E_SSH_PORT      the loopback port sshd is published on
#   PT_E2E_SSH_STATE     where the throwaway key and known_hosts live
#
# The key is made here, for this double, and is the only one it trusts. Nothing
# of the user's own ~/.ssh is mounted, read or written.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
STATE=${PT_E2E_SSH_STATE:-$HOME/.cache/odoo-sheller-ssh-double}
BASE=${PT_E2E_SSH_BASE:-odoo19-quickbooks-compose-odoo}
DB_FROM=${PT_E2E_SSH_DB_FROM:-qbo19}
NETWORK=${PT_E2E_SSH_NETWORK:-odoo-shared}
ODOO_SRC=${PT_E2E_SSH_ODOO_SRC:-/opt/odoo-src/odoo19}
PORT=${PT_E2E_SSH_PORT:-2222}
NAME=odoo-sheller-ssh-double

mkdir -p "$STATE"
chmod 700 "$STATE"
[ -f "$STATE/key" ] || ssh-keygen -q -t ed25519 -N '' -C 'sheller-double-throwaway' -f "$STATE/key"

docker build -q --build-arg "BASE=$BASE" -t "$NAME" "$HERE" >/dev/null
docker rm -f "$NAME" >/dev/null 2>&1 || true
# A new container has a new host key; the old one is no longer true.
rm -f "$STATE/known_hosts"

ENVFILE=$(mktemp)
trap 'rm -f "$ENVFILE"' EXIT
chmod 600 "$ENVFILE"
docker inspect "$DB_FROM" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^DB_' > "$ENVFILE"

docker run -d --name "$NAME" --network "$NETWORK" -p "127.0.0.1:$PORT:22" \
  --env-file "$ENVFILE" \
  -e "SSH_AUTHORIZED_KEY=$(cat "$STATE/key.pub")" \
  -e "ODOO_ADDONS_PATH=${PT_E2E_SSH_ADDONS:-/opt/odoo/odoo/addons,/opt/odoo/addons}" \
  -v "$ODOO_SRC:/opt/odoo:ro" \
  "$NAME" >/dev/null

# sshd is ready when it answers a key login.
i=0
until ssh -T -o BatchMode=yes -o ConnectTimeout=2 -o IdentitiesOnly=yes \
      -o StrictHostKeyChecking=accept-new -o "UserKnownHostsFile=$STATE/known_hosts" \
      -i "$STATE/key" -p "$PORT" ubuntu@127.0.0.1 true 2>/dev/null; do
  i=$((i + 1))
  [ "$i" -lt 30 ] || { echo "sshd did not come up" >&2; docker logs "$NAME" >&2; exit 1; }
  sleep 1
done
echo "$NAME is up on 127.0.0.1:$PORT; key and known_hosts are in $STATE"
