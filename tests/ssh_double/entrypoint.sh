#!/bin/sh
# Starts sshd for the `ubuntu` user and writes the one config file a server
# like this has: readable only by `odoo`, so a session that is not `odoo`
# cannot read it — which is how the real server we modelled this on behaves.
#
# Everything arrives as environment; nothing secret is baked into the image.
#   SSH_AUTHORIZED_KEY  the public key `ubuntu` logs in with
#   DB_HOST DB_USER DB_PASSWORD   the database Odoo is to use
#   ODOO_ADDONS_PATH    comma-separated, as in odoo.conf
set -eu

: "${SSH_AUTHORIZED_KEY:?}" "${DB_HOST:?}" "${DB_USER:?}" "${DB_PASSWORD:?}" "${ODOO_ADDONS_PATH:?}"

umask 077
install -d -m 0700 -o ubuntu -g ubuntu /home/ubuntu/.ssh
printf '%s\n' "$SSH_AUTHORIZED_KEY" > /home/ubuntu/.ssh/authorized_keys
chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys
chmod 0600 /home/ubuntu/.ssh/authorized_keys

# `workers = 2` is what a server that is actually in use has, and it matters: with
# workers above 0 Odoo's shell takes the prefork path, which binds the HTTP port
# before the shell starts. Without it this double would hide the one thing that
# made a recipe work by hand and fail in a session.
cat > /etc/odoo/odoo.conf <<CONF
[options]
db_host = ${DB_HOST}
db_user = ${DB_USER}
db_password = ${DB_PASSWORD}
addons_path = ${ODOO_ADDONS_PATH}
workers = 2
http_port = 8069
CONF
chown odoo:odoo /etc/odoo/odoo.conf
chmod 0640 /etc/odoo/odoo.conf

ssh-keygen -A
cat > /etc/ssh/sshd_config.d/10-double.conf <<CONF
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AllowUsers ubuntu
CONF

# A stand-in for the service that is already running: something listening where
# Odoo's own HTTP port is, as on any server in use. A shell that tries to bind it
# dies with "Address already in use", as it does on the real one.
python3 -c '
import socket, time
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("", 8069))
s.listen()
time.sleep(10 ** 9)
' &

exec /usr/sbin/sshd -D -e
