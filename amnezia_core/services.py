"""Amnezia services that are not VPN protocols: AmneziaDNS, SFTP file sharing, a site in Tor.

Follows upstream server_scripts/{dns,sftp,website_tor}/, SftpInstaller and TorInstaller: the app
keeps no file of its own for them, it reads the SFTP account from the `docker run` command of the
container and the onion address from the Tor hidden service.
"""

import secrets
import string
from dataclasses import dataclass

from amnezia_core.errors import ConfigFormatError

DNS_CONTAINER = "amnezia-dns"
SFTP_CONTAINER = "amnezia-sftp"
TOR_CONTAINER = "amnezia-torwebsite"

# AmneziaDNS answers only inside amnezia-dns-net, at the address the VPN containers use.
DNS_PORT = 53
DNS_ADDRESS = "172.29.172.254"
# Unbound includes it at the end of server:, so its options win; the panel turns the logs off here.
DNS_FORWARD_RECORDS_PATH = "/opt/unbound/etc/unbound/forward-records.conf"
_DNS_LOGS_OFF = ("log-queries: no", "log-replies: no")

SFTP_PORT = 222
SFTP_USER = "sftp_user"
# Utils::getRandomString(16) of SftpInstaller::generateConfig.
SFTP_PASSWORD_LENGTH = 16
_PASSWORD_ALPHABET = string.ascii_letters + string.digits
# What SftpInstaller::extractConfigFromContainer asks docker for.
SFTP_COMMAND_FORMAT = "{{.Config.Cmd}}"
# atmoz/sftp keeps the uploads of each account in /home/<login>/upload.
SFTP_HOME = "/home"

ONION_PATH = "/var/lib/tor/hidden_service/hostname"
# The WordPress site of amneziavpn/torpress and its MariaDB database, with the account the
# supervisord.conf of the image gives every container.
TOR_SITE_DIR = "/var/www/html"
TOR_DATABASE = "wp"
TOR_DATABASE_USER = "wpuser"
TOR_DATABASE_PASSWORD = "wppass"


@dataclass(frozen=True, repr=False)
class SftpAccount:
    """The one account of amnezia-sftp; `repr` hides the password, a secret."""

    user: str
    password: str

    @classmethod
    def generate(cls) -> "SftpAccount":
        password = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(SFTP_PASSWORD_LENGTH))
        return cls(SFTP_USER, password)

    @classmethod
    def from_command(cls, output: str) -> "SftpAccount":
        """The account in `docker inspect` of the command: `[user:password:::upload]`."""
        user, _, rest = output.strip().strip("[]").partition(":")
        password = rest.partition(":")[0]
        if not user.strip() or not password.strip():
            raise ConfigFormatError("the amnezia-sftp container has no user:password argument")
        return cls(user.strip(), password.strip())

    @property
    def argument(self) -> str:
        """The argument of atmoz/sftp in upstream run_container.sh: an `upload` folder."""
        return f"{self.user}:{self.password}:::upload"


def dns_logs_off(forward_records: str) -> bool:
    """forward-records.conf turns the query and reply logs off, as the panel's Dockerfile writes."""
    lines = {line.strip() for line in forward_records.splitlines()}
    return all(option in lines for option in _DNS_LOGS_OFF)


def parse_onion(output: str) -> str:
    """The onion address in the hostname file of the hidden service, as TorInstaller reads it."""
    onion = output.replace("\n", "").strip()
    if not onion:
        raise ConfigFormatError("the Tor hidden service has no hostname yet")
    return onion
