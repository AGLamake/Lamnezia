"""What a user gets for an IKEv2 client: vpn:// for the app, .sswan and .mobileconfig.

Mirrors upstream Ikev2Configurator (genIkev2Config, genStrongSwanConfig, genMobileConfig) and
Ikev2ClientConfig/Ikev2ServerConfig::toJson. The PKCS#12 of the client is the one pk12util
exports in the container, with an empty password as in the app.

Upstream genMobileConfig encodes the CA in base64 twice, though ca_cert_base64.p12 is base64
already; here it is encoded once, so that iOS reads the certificate. The PKCS#12 payload also
carries the empty password, so that iOS does not ask for one.
"""

import textwrap
import uuid
from dataclasses import dataclass
from typing import Any

from amnezia_core.ipsec import IPSEC_CONTAINER, IPSEC_PROTOCOL
from amnezia_core.qt_json import to_qt_compact_json, to_qt_json
from amnezia_core.script_vars import substitute
from amnezia_core.vpn_link import DEFAULT_DNS, connection_config

# QUuid::createUuid() upstream; the panel derives them from the client, so that a profile
# downloaded again replaces the installed one instead of adding a second.
_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/lamnezia/ipsec-profile")
_UUID_GEN = "$(UUID_GEN)"
_BASE64_LINE = 64

# Verbatim copy of amnezia-client client/server_scripts/ipsec/strongswan.profile.
_STRONGSWAN_TEMPLATE = """\
{
  "uuid": "$UUID",
  "name": "IKEv2 VPN ($SERVER_ADDR)",
  "type": "ikev2-cert",
  "remote": {
    "addr": "$SERVER_ADDR"
  },
  "local": {
    "p12": "$P12_BASE64",
    "rsa-pss": "true"
  },
  "ike-proposal": "aes256-sha256-modp2048",
  "esp-proposal": "aes128gcm16"
}"""

# Verbatim copy of amnezia-client client/server_scripts/ipsec/mobileconfig.plist.
_MOBILECONFIG_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>PayloadContent</key>
  <array>
    <dict>
      <key>IKEv2</key>
      <dict>
        <key>AuthenticationMethod</key>
        <string>Certificate</string>
        <key>ChildSecurityAssociationParameters</key>
        <dict>
          <key>DiffieHellmanGroup</key>
          <integer>14</integer>
          <key>EncryptionAlgorithm</key>
          <string>AES-128-GCM</string>
          <key>LifeTimeInMinutes</key>
          <integer>1410</integer>
        </dict>
        <key>DeadPeerDetectionRate</key>
        <string>Medium</string>
        <key>DisableRedirect</key>
        <true/>
        <key>EnableCertificateRevocationCheck</key>
        <integer>0</integer>
        <key>EnablePFS</key>
        <integer>0</integer>
        <key>IKESecurityAssociationParameters</key>
        <dict>
          <key>DiffieHellmanGroup</key>
          <integer>14</integer>
          <key>EncryptionAlgorithm</key>
          <string>AES-256</string>
          <key>IntegrityAlgorithm</key>
          <string>SHA2-256</string>
          <key>LifeTimeInMinutes</key>
          <integer>1410</integer>
        </dict>
        <key>LocalIdentifier</key>
        <string>$CLIENT_NAME</string>
        <key>PayloadCertificateUUID</key>
        <string>$UUID1</string>
        <key>OnDemandEnabled</key>
        <integer>0</integer>
        <key>OnDemandRules</key>
        <array>
          <dict>
          <key>Action</key>
          <string>Connect</string>
          </dict>
        </array>
        <key>RemoteAddress</key>
        <string>$SERVER_ADDR</string>
        <key>RemoteIdentifier</key>
        <string>$SERVER_ADDR</string>
        <key>UseConfigurationAttributeInternalIPSubnet</key>
        <integer>0</integer>
      </dict>
      <key>IPv4</key>
      <dict>
        <key>OverridePrimary</key>
        <integer>1</integer>
      </dict>
      <key>PayloadDescription</key>
      <string>Configures VPN settings</string>
      <key>PayloadDisplayName</key>
      <string>VPN</string>
      <key>PayloadOrganization</key>
      <string>IKEv2 VPN</string>
      <key>PayloadIdentifier</key>
      <string>com.apple.vpn.managed.$(UUID_GEN)</string>
      <key>PayloadType</key>
      <string>com.apple.vpn.managed</string>
      <key>PayloadUUID</key>
      <string>$(UUID_GEN)</string>
      <key>PayloadVersion</key>
      <integer>1</integer>
      <key>Proxies</key>
      <dict>
        <key>HTTPEnable</key>
        <integer>0</integer>
        <key>HTTPSEnable</key>
        <integer>0</integer>
      </dict>
      <key>UserDefinedName</key>
      <string>$SERVER_ADDR</string>
      <key>VPNType</key>
      <string>IKEv2</string>
    </dict>
    <dict>
      <key>PayloadCertificateFileName</key>
      <string>$CLIENT_NAME</string>
      <key>PayloadContent</key>
      <data>
$P12_BASE64
      </data>
      <key>PayloadDescription</key>
      <string>Adds a PKCS#12-formatted certificate</string>
      <key>PayloadDisplayName</key>
      <string>$CLIENT_NAME</string>
      <key>PayloadIdentifier</key>
      <string>com.apple.security.pkcs12.$(UUID_GEN)</string>
      <key>PayloadType</key>
      <string>com.apple.security.pkcs12</string>
      <key>PayloadUUID</key>
      <string>$UUID1</string>
      <key>PayloadVersion</key>
      <integer>1</integer>
    </dict>
    <dict>
      <key>PayloadContent</key>
      <data>
$CA_BASE64
      </data>
      <key>PayloadCertificateFileName</key>
      <string>ikev2vpnca</string>
      <key>PayloadDescription</key>
      <string>Adds a CA root certificate</string>
      <key>PayloadDisplayName</key>
      <string>Certificate Authority (CA)</string>
      <key>PayloadIdentifier</key>
      <string>com.apple.security.root.$(UUID_GEN)</string>
      <key>PayloadType</key>
      <string>com.apple.security.root</string>
      <key>PayloadUUID</key>
      <string>$(UUID_GEN)</string>
      <key>PayloadVersion</key>
      <integer>1</integer>
    </dict>
  </array>
  <key>PayloadDisplayName</key>
  <string>IKEv2 VPN ($SERVER_ADDR)</string>
  <key>PayloadIdentifier</key>
  <string>com.apple.vpn.managed.$(UUID_GEN)</string>
  <key>PayloadRemovalDisallowed</key>
  <false/>
  <key>PayloadType</key>
  <string>Configuration</string>
  <key>PayloadUUID</key>
  <string>$(UUID_GEN)</string>
  <key>PayloadVersion</key>
  <integer>1</integer>
</dict>
</plist>
"""
# The PKCS#12 payload of the template, and the same with the password of the PKCS#12.
_P12_PAYLOAD_START = """\
      <key>PayloadCertificateFileName</key>
      <string>$CLIENT_NAME</string>
"""
_P12_PAYLOAD_WITH_PASSWORD = (
    _P12_PAYLOAD_START + "      <key>Password</key>\n      <string>$P12_PASSWORD</string>\n"
)


@dataclass(frozen=True, repr=False)
class IpsecClientProfile:
    """Everything needed to hand an IKEv2 client to a user.

    `host` is the server address the app connects to; `server_ip` is the identity of the
    server certificate (leftid), which strongSwan and iOS check. `p12` is base64 of the
    client's PKCS#12 with its private key, the user's credential; `repr` hides it.
    """

    host: str
    server_ip: str
    client_id: str
    p12: str
    ca_cert: str
    password: str = ""

    def __repr__(self) -> str:
        return f"IpsecClientProfile(host={self.host!r}, client_id={self.client_id!r})"

    def native_config(self) -> str:
        """Ikev2Configurator::genIkev2Config: what the Windows app reads as ikev2_config_data."""
        config = {
            "hostName": self.host,
            "userName": self.client_id,
            "cert": self.p12,
            "password": self.password,
        }
        return to_qt_json(config)

    def vpn_config(
        self, description: str = "", dns: tuple[str, str] = DEFAULT_DNS
    ) -> dict[str, Any]:
        """Server JSON for vpn://; `last_config` is Ikev2ClientConfig::toJson as compact text."""
        client = {
            "config": self.native_config(),
            "hostName": self.host,
            "userName": self.client_id,
            "cert": self.p12,
            "password": self.password,
            "clientId": self.client_id,
        }
        client = {key: value for key, value in client.items() if value != ""}
        container = {
            "container": IPSEC_CONTAINER,
            IPSEC_PROTOCOL: {"hostName": self.host, "last_config": to_qt_compact_json(client)},
        }
        return connection_config(self.host, container, description, dns)

    def sswan_file(self) -> str:
        """genStrongSwanConfig: the profile of the strongSwan app for Android."""
        variables = {
            "$UUID": str(self._uuid("sswan")),
            "$SERVER_ADDR": self.server_ip,
            "$P12_BASE64": "".join(self.p12.split()),
        }
        return substitute(_STRONGSWAN_TEMPLATE, variables) + "\n"

    def mobileconfig_file(self) -> str:
        """genMobileConfig: the configuration profile for iPhone, iPad and Mac."""
        template = _MOBILECONFIG_TEMPLATE.replace(_P12_PAYLOAD_START, _P12_PAYLOAD_WITH_PASSWORD)
        for number in range(template.count(_UUID_GEN)):
            template = template.replace(_UUID_GEN, str(self._uuid(f"payload-{number}")), 1)
        variables = {
            "$CLIENT_NAME": self.client_id,
            "$UUID1": str(self._uuid("p12")),
            "$SERVER_ADDR": self.server_ip,
            "$P12_BASE64": _wrapped(self.p12),
            "$CA_BASE64": _wrapped(self.ca_cert),
            "$P12_PASSWORD": self.password,
        }
        return substitute(template, variables)

    def _uuid(self, purpose: str) -> uuid.UUID:
        return uuid.uuid5(_UUID_NAMESPACE, f"{self.server_ip}/{self.client_id}/{purpose}")


def _wrapped(base64_text: str) -> str:
    return "\n".join(textwrap.wrap("".join(base64_text.split()), _BASE64_LINE))
