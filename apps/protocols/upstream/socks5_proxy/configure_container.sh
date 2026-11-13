#!/bin/sh
# From amnezia-client client/server_scripts/socks5_proxy/configure_container.sh (commit 7f9b7db).
# Copyright (C) Amnezia VPN contributors, GPL-3.0 (see NOTICE).
# Changed: no `log` and `logformat` lines. Upstream logs every requested host to
# /usr/local/3proxy/logs/3proxy.log; the panel does not log the sites users visit.
# The app does not read that log, so it sees the proxy as before.

echo -e "#!/bin/3proxy" > /usr/local/3proxy/conf/3proxy.cfg
echo -e "config /usr/local/3proxy/conf/3proxy.cfg" >> /usr/local/3proxy/conf/3proxy.cfg
echo -e "timeouts 1 5 30 60 180 1800 15 60" >> /usr/local/3proxy/conf/3proxy.cfg

echo -e "$SOCKS5_USER" >> /usr/local/3proxy/conf/3proxy.cfg

echo -e "auth $SOCKS5_AUTH_TYPE" >> /usr/local/3proxy/conf/3proxy.cfg
echo -e "socks -p$SOCKS5_PROXY_PORT" >> /usr/local/3proxy/conf/3proxy.cfg