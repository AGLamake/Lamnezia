"""Settings for pytest and mypy: work without a .env file."""

import os

os.environ.setdefault("LAMNEZIA_SECRET_KEY", "insecure-test-only-key")
os.environ.setdefault("LAMNEZIA_ENCRYPTION_KEY", "Z11A4lsplCmTUdlaLWFK6MYf4QOAO1ET565RjiGSuP8=")

from config.settings import *  # noqa: F403

# Fast hashing keeps user-creating tests quick; never used outside tests.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Tests render templates without running collectstatic.
STORAGES = {
    **STORAGES,  # noqa: F405
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
# The WebSocket origin check reads ALLOWED_HOSTS once, when config.asgi is imported.
ALLOWED_HOSTS = ["testserver", "localhost"]
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
# Queued jobs run in the test process right away.
CELERY_TASK_ALWAYS_EAGER = True
# Page tests sign in administrators with force_login; the 2FA tests turn the requirement on.
REQUIRE_ADMIN_2FA = False
# Tests check English strings: they must not depend on whether the catalogs are compiled.
LANGUAGE_CODE = "en"
# The example payment plugin; it only works with DEBUG on, so the core runs without it.
INSTALLED_APPS = [*INSTALLED_APPS, "examples.billing_dummy"]  # noqa: F405
