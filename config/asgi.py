import os

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from django.core.asgi import get_asgi_application
from django.urls import path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# Django must be set up before anything that imports models.
django_application = get_asgi_application()

from apps.support.consumers import SupportConsumer  # noqa: E402
from config.health import PingConsumer  # noqa: E402

# django-stubs types path() for HTTP views only; Channels routes consumers through it too.
websocket_urlpatterns = [
    path("ws/ping", PingConsumer.as_asgi()),  # type: ignore[arg-type]
    path("ws/support/", SupportConsumer.as_asgi()),  # type: ignore[arg-type]
    path("ws/support/<int:pk>/", SupportConsumer.as_asgi()),  # type: ignore[arg-type]
]

application = ProtocolTypeRouter(
    {
        "http": django_application,
        # The session cookie signs the socket in; the origin check keeps other sites from using it.
        "websocket": AllowedHostsOriginValidator(
            AuthMiddlewareStack(URLRouter(websocket_urlpatterns))  # type: ignore[arg-type]
        ),
    }
)
