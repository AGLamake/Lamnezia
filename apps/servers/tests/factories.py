import factory

from apps.servers.models import AuthMethod, Server

PRIVATE_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret-key-body\n-----END OPENSSH PRIVATE KEY-----\n"
)


class ServerFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Server

    name = factory.Sequence(lambda n: f"server-{n}")
    host = factory.Sequence(lambda n: f"203.0.113.{n % 250 + 1}")
    auth_method = AuthMethod.KEY
    private_key = PRIVATE_KEY
