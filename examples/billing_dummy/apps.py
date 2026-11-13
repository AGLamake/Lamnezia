from django.apps import AppConfig


class BillingDummyConfig(AppConfig):
    name = "examples.billing_dummy"

    def ready(self) -> None:
        from apps.billing.services.providers import payment_providers  # noqa: PLC0415
        from examples.billing_dummy.provider import DummyProvider  # noqa: PLC0415

        payment_providers.register(DummyProvider())
