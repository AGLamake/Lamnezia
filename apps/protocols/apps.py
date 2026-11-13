from django.apps import AppConfig


class ProtocolsConfig(AppConfig):
    name = "apps.protocols"

    def ready(self) -> None:
        # Imported for their side effect: they register operation kinds, providers, installers.
        from apps.protocols import operations, providers, receivers  # noqa: F401, PLC0415
        from apps.protocols.services import (  # noqa: F401, PLC0415
            awg_installer,
            ipsec_installer,
            openvpn_installer,
            proxy_installers,
            service_installers,
            tproxy_installer,
            xray_installer,
        )

        receivers.connect()
