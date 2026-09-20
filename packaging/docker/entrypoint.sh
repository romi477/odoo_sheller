#!/bin/sh
# Start the daemon in a container, as somebody who can both write the
# journals and reach the Engine.
#
# Those are two different identities and they come from two different places.
# The journals go to a bind mount of the host's ~/.odoo-sheller, so they have
# to be written by the host user who will later read them with a natively
# installed daemon. The Engine is reached through a socket owned by a group
# that has nothing to do with that user. So: the mount's owner becomes the
# process identity, the socket's group is added on top of it.
#
# Portable on purpose. `stat -c` is GNU and `stat -f` is BSD; the image has the
# first, a developer running the tests on macOS has the second.
set -eu

SOCKET="${ODOO_SHELLER_SOCKET:-/var/run/docker.sock}"
STATE="${ODOO_SHELLER_STATE:-/data/.odoo-sheller}"
PORT="${ODOO_SHELLER_PORT:-8765}"
RUN_USER=sheller

owner_uid() { stat -c %u "$1" 2>/dev/null || stat -f %u "$1"; }
owner_gid() { stat -c %g "$1" 2>/dev/null || stat -f %g "$1"; }

if [ ! -S "$SOCKET" ]; then
    echo "odoo-sheller: no docker socket at $SOCKET" >&2
    echo "  mount the host's: -v /var/run/docker.sock:/var/run/docker.sock" >&2
    echo "  rootless Docker keeps it under \$XDG_RUNTIME_DIR" >&2
    exit 1
fi

socket_gid="$(owner_gid "$SOCKET")"

# Nothing to derive an identity from, and none was given. Refusing is the
# only honest answer: this script cannot tell a Linux host — where starting
# anyway means root-owned journals that a natively installed daemon can never
# append to — from a macOS one, where it would have been harmless. One rule
# for both beats guessing at the host.
if [ ! -d "$STATE" ] && [ -z "${ODOO_SHELLER_UID:-}" ]; then
    echo "odoo-sheller: $STATE does not exist and ODOO_SHELLER_UID is unset." >&2
    echo "  Starting anyway would write journals as root, and a daemon" >&2
    echo "  installed natively on this host could never append to them." >&2
    echo "  Either:" >&2
    echo "    -e ODOO_SHELLER_UID=\$(id -u) -e ODOO_SHELLER_GID=\$(id -g)" >&2
    echo "    or create ~/.odoo-sheller on the host before starting this." >&2
    exit 2
fi

created=no
if [ ! -d "$STATE" ]; then
    mkdir -p "$STATE"
    created=yes
fi

uid="${ODOO_SHELLER_UID:-$(owner_uid "$STATE")}"
gid="${ODOO_SHELLER_GID:-$(owner_gid "$STATE")}"

# A directory this script made belongs to whoever is about to write in it.
# Never touch one that was already there: that is the host's bind mount, and
# its ownership is the host's answer, not ours to overrule.
if [ "$created" = yes ] && [ "$(id -u)" = 0 ]; then
    chown "$uid:$gid" "$STATE"
fi

# uid 0 is not a mistake to correct. It is what a mount reports when the host
# fakes ownership: macOS presents every bind-mounted file as owned by the
# container's own uid, and rootless Docker maps the host user to 0 inside and
# unwinds it on the way out. Both write correctly as root. Reached only when
# the uid was derived rather than given — a caller passing ODOO_SHELLER_UID,
# as the documented command does, lands below instead, which is equally fine
# on those hosts.
drop=yes
if [ "$uid" = 0 ]; then
    drop=no
fi

echo "odoo-sheller: uid=$uid gid=$gid socket_gid=$socket_gid drop=$drop"

if [ "${ODOO_SHELLER_DRY_RUN:-}" = 1 ]; then
    exit 0
fi

if [ "$drop" = no ]; then
    exec odoo-sheller --host 0.0.0.0 --port "$PORT"
fi

# The uid may already belong to somebody in the image — Debian ships a dozen
# accounts below 1000 — and useradd refuses a duplicate. Adopt that account
# instead of dying on it; what matters is the number, not the name.
existing="$(getent passwd "$uid" | cut -d: -f1)"
if [ -n "$existing" ]; then
    RUN_USER="$existing"
else
    getent group "$gid" >/dev/null 2>&1 || groupadd -g "$gid" "$RUN_USER"
    useradd -u "$uid" -g "$gid" -M -d "$HOME" -s /usr/sbin/nologin "$RUN_USER"
fi

# An existing group keeps its name: creating a second one for the same gid
# would work and then confuse everything that reads the image later.
socket_group="$(getent group "$socket_gid" | cut -d: -f1)"
if [ -z "$socket_group" ]; then
    socket_group=dockerhost
    groupadd -g "$socket_gid" "$socket_group"
fi
usermod -aG "$socket_group" "$RUN_USER"

# env HOME=, because gosu takes HOME from the account it switches to. An
# adopted account carries its own home — www-data lives in /var/www — and
# the daemon builds every state path from Path.home(), so without this the
# journals leave the mount without a word.
exec gosu "$RUN_USER" env HOME="$HOME" odoo-sheller --host 0.0.0.0 --port "$PORT"
