# From amnezia-client client/server_scripts/dns/Dockerfile.
# Copyright (C) Amnezia VPN contributors, GPL-3.0 (see NOTICE).
# Changed: the first lines of forward-records.conf turn the query and reply logs off. The
# unbound.conf of mvance/unbound already keeps them off; the image is `latest`, so the panel says
# it again. The file is included at the end of the server: clause, so these lines win.
# The app does not read this file, so it sees the service as before.
FROM mvance/unbound:latest

LABEL maintainer="AmneziaVPN"

RUN echo " \n\
verbosity: 0\n\
log-queries: no\n\
log-replies: no\n\
log-tag-queryreply: no\n\
log-local-actions: no\n\
log-servfail: no\n\
\n\
domain-insecure: \"coin.\"\n\
domain-insecure: \"emc.\"\n\
domain-insecure: \"lib.\"\n\
domain-insecure: \"bazar.\"\n\
domain-insecure: \"enum.\"\n\
\n\
stub-zone:\n\
   name: coin.\n\
   stub-host: seed1.emercoin.com\n\
   stub-host: seed2.emercoin.com\n\
   stub-first: yes\n\
\n\
stub-zone:\n\
   name: emc.\n\
   stub-host: seed1.emercoin.com\n\
   stub-host: seed2.emercoin.com\n\
   stub-first: yes\n\
\n\
stub-zone:\n\
   name: lib.\n\
   stub-host: seed1.emercoin.com\n\
   stub-host: seed2.emercoin.com\n\
   stub-first: yes\n\
\n\
stub-zone:\n\
   name: bazar.\n\
   stub-host: seed1.emercoin.com\n\
   stub-host: seed2.emercoin.com\n\
   stub-first: yes\n\
\n\
stub-zone:\n\
   name: enum.\n\
   stub-host: seed1.emercoin.com\n\
   stub-host: seed2.emercoin.com\n\
   stub-first: yes\n\
\n\
forward-zone:\n\
   name: .\n\
   forward-tls-upstream: yes\n\
   forward-addr: 1.1.1.1@853  #cloudflare-dns.com\n\
   forward-addr: 1.0.0.1@853  #cloudflare-dns.com\n\
" | tee /opt/unbound/etc/unbound/forward-records.conf

