# A stand-in for the server the `ssh` target kind is for: Odoo installed in the
# system rather than in a container of its own, a login user who may become
# the `odoo` user with sudo, and an sshd that takes keys and nothing else.
#
# It exists because the one thing a shim cannot reproduce is the real thing:
# the real `ssh` joining and re-parsing its arguments, and the real `sudo`
# closing every file descriptor above 2 — which is why the script that does
# `exec 3<&0` has to run *inside* the user switch, never before it. Only the
# base needs to be an image that already has Odoo's Python dependencies
# installed; the Odoo source is mounted at run time, as it is for a local
# container.
ARG BASE=odoo19-quickbooks-compose-odoo
FROM ${BASE}

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-server sudo \
 && rm -rf /var/lib/apt/lists/* \
 # `*`, not `!`: a locked account is refused by sshd even for a key.
 && useradd --create-home --shell /bin/bash --password '*' ubuntu \
 # Becoming `odoo` and nothing else, the way a deploy user usually can.
 && echo 'ubuntu ALL=(odoo) NOPASSWD: ALL' > /etc/sudoers.d/ubuntu \
 && chmod 0440 /etc/sudoers.d/ubuntu \
 && mkdir -p /run/sshd /etc/odoo \
 # `sudo -H` resets the environment, so `import odoo` cannot lean on a
 # PYTHONPATH the way a container's own entrypoint does. A server with Odoo
 # installed in the system finds it through site-packages; so does this one.
 && python3 -c "import site; print(site.getsitepackages()[0])" > /tmp/site \
 && echo /opt/odoo > "$(cat /tmp/site)/odoo-source.pth" \
 && rm /tmp/site

COPY entrypoint.sh /usr/local/bin/ssh-double-entrypoint
RUN chmod 0755 /usr/local/bin/ssh-double-entrypoint

EXPOSE 22
ENTRYPOINT ["/usr/local/bin/ssh-double-entrypoint"]
