# Test SSH server: like a VPS prepared by Amnezia, with sudo, flock and the docker CLI.
# The docker socket of the CI host is mounted, so `docker exec` reaches sibling containers.
FROM docker:27-cli AS docker-cli

FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server sudo \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/sshd \
    && useradd -m -s /bin/bash -G sudo admin \
    && echo "admin ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/admin \
    && useradd -m -s /bin/bash plain \
    && echo "plain:plain-pass" | chpasswd \
    && echo "PasswordAuthentication yes" > /etc/ssh/sshd_config.d/test.conf
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY entrypoint.sh /entrypoint.sh
EXPOSE 22
ENTRYPOINT ["sh", "/entrypoint.sh"]
