from django.apps import AppConfig


class PlansConfig(AppConfig):
    name = "apps.plans"

    def ready(self) -> None:
        from apps.plans import receivers  # noqa: F401, PLC0415  plans follow server groups
