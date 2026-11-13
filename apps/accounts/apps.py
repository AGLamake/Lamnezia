from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "apps.accounts"

    def ready(self) -> None:
        # Records sign-ins for the list of sessions in the profile.
        from apps.accounts.services import sessions  # noqa: PLC0415

        sessions.connect()
