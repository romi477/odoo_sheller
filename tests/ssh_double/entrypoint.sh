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

cat > /etc/odoo/odoo.conf <<CONF
[options]
db_host = ${DB_HOST}
db_user = ${DB_USER}
db_password = ${DB_PASSWORD}
addons_path = ${ODOO_ADDONS_PATH}
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

exec /usr/sbin/sshd -D -e
