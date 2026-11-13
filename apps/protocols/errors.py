"""Errors of preparing a server and installing protocols, with a message and a hint."""

from enum import StrEnum

from django.utils.translation import gettext_lazy as _

from apps.ssh.errors import CatalogError, ErrorDescription


class SetupErrorCode(StrEnum):
    UNSUPPORTED_OS = "unsupported_os"
    PACKAGE_MANAGER_BUSY = "package_manager_busy"
    PACKAGE_INSTALL_FAILED = "package_install_failed"
    DOCKER_NOT_RUNNING = "docker_not_running"
    PORT_BUSY = "port_busy"
    DOCKER_PULL_LIMIT = "docker_pull_limit"
    IMAGE_BUILD_FAILED = "image_build_failed"
    KERNEL_TOO_OLD = "kernel_too_old"
    CONTAINER_START_FAILED = "container_start_failed"
    AWG_NOT_RUNNING = "awg_not_running"
    AWG3_DROPPED = "awg3_dropped"
    PREVIOUS_CONTAINER_LEFT = "previous_container_left"
    CONTAINER_NOT_REMOVED = "container_not_removed"
    DATA_VOLUME_LEFT = "data_volume_left"
    XRAY_KEYGEN_FAILED = "xray_keygen_failed"
    XRAY_CONFIG_INVALID = "xray_config_invalid"
    XRAY_NOT_RUNNING = "xray_not_running"
    HOST_NOT_RESOLVED = "host_not_resolved"
    OPENVPN_PKI_FAILED = "openvpn_pki_failed"
    OPENVPN_CONFIG_INVALID = "openvpn_config_invalid"
    OPENVPN_NOT_RUNNING = "openvpn_not_running"
    OPENVPN_PKI_NOT_COPIED = "openvpn_pki_not_copied"
    IPSEC_SETUP_FAILED = "ipsec_setup_failed"
    IPSEC_CONFIG_INVALID = "ipsec_config_invalid"
    IPSEC_NOT_RUNNING = "ipsec_not_running"
    IPSEC_CERTIFICATE_FAILED = "ipsec_certificate_failed"
    TOR_NO_ONION = "tor_no_onion"
    PROXY_CONFIGURE_FAILED = "proxy_configure_failed"
    PROXY_NOT_RUNNING = "proxy_not_running"
    SOCKS5_NOT_RUNNING = "socks5_not_running"
    SOCKS5_NO_ACCOUNT = "socks5_no_account"
    DNS_NOT_RUNNING = "dns_not_running"
    TPROXY_DOMAIN_UNRESOLVED = "tproxy_domain_unresolved"
    TPROXY_DOMAIN_ELSEWHERE = "tproxy_domain_elsewhere"
    TPROXY_HTTP_PORT_BUSY = "tproxy_http_port_busy"
    NO_SYSTEMD = "no_systemd"
    OFFLOAD_TOOLS_FAILED = "offload_tools_failed"
    SPEED_TEST_PORT_BUSY = "speed_test_port_busy"
    SPEED_TEST_FAILED = "speed_test_failed"
    MIGRATION_TARGET_REMOVED = "migration_target_removed"
    MIGRATION_TARGET_OUTDATED = "migration_target_outdated"
    MIGRATION_TARGET_MISSING = "migration_target_missing"
    MIGRATION_NOT_DONE = "migration_not_done"


SETUP_ERRORS: dict[SetupErrorCode, ErrorDescription] = {
    SetupErrorCode.UNSUPPORTED_OS: ErrorDescription(
        _("The operating system of the server is not supported."),
        _("The panel prepares Debian and Ubuntu servers. Use one of them or the Amnezia app."),
    ),
    SetupErrorCode.PACKAGE_MANAGER_BUSY: ErrorDescription(
        _("The package manager on the server is busy."),
        _("Automatic updates are probably running. Wait a few minutes and try again."),
    ),
    SetupErrorCode.PACKAGE_INSTALL_FAILED: ErrorDescription(
        _("Docker could not be installed."),
        _("See the step log. Check that the server has internet access and free disk space."),
    ),
    SetupErrorCode.DOCKER_NOT_RUNNING: ErrorDescription(
        _("Docker is installed but does not run."),
        _("Check the output of `systemctl status docker` on the server."),
    ),
    SetupErrorCode.PORT_BUSY: ErrorDescription(
        _("The VPN port is already used by another program on the server."),
        _("Choose another port or stop the program that uses it."),
    ),
    SetupErrorCode.DOCKER_PULL_LIMIT: ErrorDescription(
        _("Docker Hub refused to download the image: the download limit is reached."),
        _("Wait an hour and try again."),
    ),
    SetupErrorCode.IMAGE_BUILD_FAILED: ErrorDescription(
        _("The protocol container could not be built."),
        _("See the step log. Check that the server can reach Docker Hub and has free disk space."),
    ),
    SetupErrorCode.KERNEL_TOO_OLD: ErrorDescription(
        _("The Linux kernel of the server is too old for AmneziaWG."),
        _("AmneziaWG needs Linux 4.14 or newer. Upgrade the system or use another server."),
    ),
    SetupErrorCode.CONTAINER_START_FAILED: ErrorDescription(
        _("The protocol container could not be started."),
        _("See the step log for the Docker error."),
    ),
    SetupErrorCode.AWG_NOT_RUNNING: ErrorDescription(
        _("AmneziaWG did not start in the container."),
        _("Check the container log: docker logs amnezia-awg2."),
    ),
    SetupErrorCode.AWG3_DROPPED: ErrorDescription(
        _("AmneziaWG started without the settings of version 3.1."),
        _(
            "The server probably has an old amneziawg kernel module: it drops the new settings "
            "without an error. Update or unload the module and try again. The server stays on "
            "AmneziaWG 2.0."
        ),
    ),
    SetupErrorCode.PREVIOUS_CONTAINER_LEFT: ErrorDescription(
        _("The server still has the container left by an interrupted change of the settings."),
        _(
            "Compare the container and the same name with -previous in `docker ps -a`, "
            "remove the one that is not needed and try again."
        ),
    ),
    SetupErrorCode.CONTAINER_NOT_REMOVED: ErrorDescription(
        _("The container is still on the server after the removal."),
        _("See the step log and `docker ps -a` on the server, then try again."),
    ),
    SetupErrorCode.DATA_VOLUME_LEFT: ErrorDescription(
        _("The container is removed, but its data volume is still on the server."),
        _("Remove it with `docker volume rm` on the server, or try again."),
    ),
    SetupErrorCode.XRAY_KEYGEN_FAILED: ErrorDescription(
        _("XRay could not generate the Reality keys in the container."),
        _("See the step log. Try again: keys that were created are kept."),
    ),
    SetupErrorCode.XRAY_CONFIG_INVALID: ErrorDescription(
        _("The container already has a server.json the panel cannot read."),
        _(
            "Fix /opt/amnezia/xray/server.json in amnezia-xray, or remove the protocol "
            "and install it again."
        ),
    ),
    SetupErrorCode.XRAY_NOT_RUNNING: ErrorDescription(
        _("XRay did not start in the container."),
        _("Check server.json and the container log: docker logs amnezia-xray."),
    ),
    SetupErrorCode.HOST_NOT_RESOLVED: ErrorDescription(
        _("The IP address of the server cannot be found from its name."),
        _("Check the DNS record of the server, or give the server by its IP address."),
    ),
    SetupErrorCode.OPENVPN_PKI_FAILED: ErrorDescription(
        _("easy-rsa could not make the OpenVPN certificate authority."),
        _("See the step log and try again: an unfinished authority is made anew."),
    ),
    SetupErrorCode.OPENVPN_CONFIG_INVALID: ErrorDescription(
        _("The container already has a server.conf the panel cannot read."),
        _(
            "Fix /opt/amnezia/openvpn/server.conf in amnezia-openvpn, or remove the protocol "
            "and install it again."
        ),
    ),
    SetupErrorCode.OPENVPN_NOT_RUNNING: ErrorDescription(
        _("OpenVPN did not start in the container."),
        _("Check server.conf and the container log: docker logs amnezia-openvpn."),
    ),
    SetupErrorCode.OPENVPN_PKI_NOT_COPIED: ErrorDescription(
        _("The certificate authority did not reach the new OpenVPN container."),
        _("The old container is back with all its clients. See the step log and try again."),
    ),
    SetupErrorCode.IPSEC_SETUP_FAILED: ErrorDescription(
        _("configure_container.sh could not make the IPsec certificate authority."),
        _("See the step log and try again: an unfinished authority is made anew."),
    ),
    SetupErrorCode.IPSEC_CONFIG_INVALID: ErrorDescription(
        _("The container already has an ikev2.conf the panel cannot read."),
        _(
            "Fix /etc/ipsec.d/ikev2.conf in amnezia-ipsec, or remove the protocol "
            "and install it again."
        ),
    ),
    SetupErrorCode.IPSEC_NOT_RUNNING: ErrorDescription(
        _("IPsec (libreswan) did not start in the container."),
        _("Check the container log: docker logs amnezia-ipsec."),
    ),
    SetupErrorCode.IPSEC_CERTIFICATE_FAILED: ErrorDescription(
        _("certutil could not issue or delete the IPsec server certificate."),
        _("See the step log and run the reissue again: finished steps are not repeated."),
    ),
    SetupErrorCode.TOR_NO_ONION: ErrorDescription(
        _("Tor did not create the onion address of the site."),
        _("Tor may need more time to start: try again, the container is kept."),
    ),
    SetupErrorCode.PROXY_CONFIGURE_FAILED: ErrorDescription(
        _("configure_container.sh could not configure the proxy."),
        _(
            "The Telegram proxies download files from core.telegram.org: check the network "
            "of the server and try again, the container is kept."
        ),
    ),
    SetupErrorCode.PROXY_NOT_RUNNING: ErrorDescription(
        _("The proxy container does not run, so its secret cannot be renewed."),
        _("Start the container from its card or read the server again, then try once more."),
    ),
    SetupErrorCode.SOCKS5_NOT_RUNNING: ErrorDescription(
        _("The SOCKS5 container does not run, so its log cannot be deleted."),
        _("Start the container from its card or read the server again, then try once more."),
    ),
    SetupErrorCode.DNS_NOT_RUNNING: ErrorDescription(
        _("The AmneziaDNS container does not run, so it is not rebuilt."),
        _("Start the container from its card or read the server again, then try once more."),
    ),
    SetupErrorCode.SOCKS5_NO_ACCOUNT: ErrorDescription(
        _("3proxy.cfg of the SOCKS5 proxy has no account, so the log is left as it is."),
        _(
            "Without the account the panel would give the proxy a new password and the app "
            "would lose it. Read the server again, or install SOCKS5 again from the panel."
        ),
    ),
    SetupErrorCode.TPROXY_DOMAIN_UNRESOLVED: ErrorDescription(
        _("The domain of the proxy has no IPv4 address in DNS."),
        _("Add an A record of the domain with the IP address of the server and try again."),
    ),
    SetupErrorCode.TPROXY_DOMAIN_ELSEWHERE: ErrorDescription(
        _("The domain of the proxy does not point at this server."),
        _(
            "Let's Encrypt gives the certificate only to the server the domain points at. Set "
            "the A record of the domain to the IP address of the server; behind the proxy of "
            "Cloudflare, turn the proxy off for this record."
        ),
    ),
    SetupErrorCode.TPROXY_HTTP_PORT_BUSY: ErrorDescription(
        _("Port 80 of the server is already used by another program."),
        _(
            "TProxy needs port 80 for its Let's Encrypt certificate. Stop the web server that "
            "uses it, then try again."
        ),
    ),
    SetupErrorCode.NO_SYSTEMD: ErrorDescription(
        _("The server has no systemd: the TSO/GSO workaround cannot be kept on."),
        _(
            "Turn TSO/GSO off by hand after every container restart: "
            "nsenter -t <pid> -n ethtool -K eth1 tso off gso off."
        ),
    ),
    SetupErrorCode.OFFLOAD_TOOLS_FAILED: ErrorDescription(
        _("ethtool could not be installed for the TSO/GSO workaround."),
        _("See the step log. Check that the server has internet access, then try again."),
    ),
    SetupErrorCode.SPEED_TEST_PORT_BUSY: ErrorDescription(
        _("One of the speed test ports is already used by another program on the server."),
        _("Choose other ports for the speed test."),
    ),
    SetupErrorCode.SPEED_TEST_FAILED: ErrorDescription(
        _("The speed test container could not be built or started."),
        _("See the step log. Check that Docker works and the server can reach Docker Hub."),
    ),
    SetupErrorCode.MIGRATION_TARGET_REMOVED: ErrorDescription(
        _("AmneziaWG 2.0 is removed from the server, its backup is kept."),
        _("Restore it in the Backup section or delete the backup, then move the clients."),
    ),
    SetupErrorCode.MIGRATION_TARGET_OUTDATED: ErrorDescription(
        _("AmneziaWG 2.0 on the server is not on version 3.1 yet."),
        _("Move AmneziaWG 2.0 to version 3.1 on the server card, then move the clients."),
    ),
    SetupErrorCode.MIGRATION_TARGET_MISSING: ErrorDescription(
        _("AmneziaWG 2.0 is installed, but the import did not find it."),
        _("Import from the server, then run the move again."),
    ),
    SetupErrorCode.MIGRATION_NOT_DONE: ErrorDescription(
        _("Not every client of the old AmneziaWG has a new key."),
        _("Move the clients first: the old container is removed only after that."),
    ),
}


class SetupError(CatalogError[SetupErrorCode]):
    """Preparing the server or installing a protocol failed."""

    catalog = SETUP_ERRORS
