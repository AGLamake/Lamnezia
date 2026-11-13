import factory

from apps.notifications.models import Channel, ChannelKind, Route


class ChannelFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Channel
        skip_postgeneration_save = True

    name = factory.Sequence(lambda n: f"Channel {n}")
    kind = ChannelKind.WEBHOOK
    target = factory.Sequence(lambda n: f"https://hooks.example.com/{n}")
    secret = "webhook secret"

    @factory.post_generation  # type: ignore[untyped-decorator]
    def events(self, create: bool, events: list[str] | None) -> None:
        for event in events or []:
            Route.objects.create(channel=self, event=event)
