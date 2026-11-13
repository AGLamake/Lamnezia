#!/bin/sh
# AUTHORIZED_KEY: public key that may log in as `admin`.
set -e
install -d -m 700 -o admin -g admin /home/admin/.ssh
echo "$AUTHORIZED_KEY" > /home/admin/.ssh/authorized_keys
chown admin:admin /home/admin/.ssh/authorized_keys
chmod 600 /home/admin/.ssh/authorized_keys
exec /usr/sbin/sshd -D -e
