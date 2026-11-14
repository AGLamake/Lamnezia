# Lamnezia relay: forwards the UDP port of a group to its live member (docs/FALLBACK.md section 5).
# amneziawg-go and awg probe the members; nftables forwards; conntrack drops old flows on a switch.
FROM amneziavpn/amneziawg-go:latest

RUN apk add --no-cache python3 nftables conntrack-tools dumb-init

# The panel writes agent.py and config.json into the folder after `docker run`; until then the
# container waits, and a restart picks up a new agent.
RUN mkdir -p /opt/amnezia/relay && chmod 700 /opt/amnezia/relay
RUN printf '%s\n' '#!/bin/sh' \
    'until [ -f /opt/amnezia/relay/agent.py ]; do sleep 1; done' \
    'exec python3 -u /opt/amnezia/relay/agent.py' > /opt/amnezia/start.sh \
    && chmod a+x /opt/amnezia/start.sh

ENTRYPOINT [ "dumb-init", "/opt/amnezia/start.sh" ]
