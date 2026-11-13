"""The user's keys in the bot: list, a key's card, a new key in steps, re-issue, delete and the
subscription link for XRay clients.

The same `MyKeys` service as the portal checks the plan, the key limit and the instances.
"""

from collections.abc import Mapping
from dataclasses import replace
from functools import cached_property

from django.utils.translation import gettext as _

from apps.bots.commands.base import CommandSet, Handler, bullets, nav, number
from apps.bots.services.delivery import KeyDelivery
from apps.bots.services.replies import Button, Command, Reply
from apps.guides.services.devices import Device
from apps.keys.models import AccessKey
from apps.keys.services.actions import KeyRevokingError, OwnerCannotHaveKeysError
from apps.keys.services.instances import instances
from apps.keys.services.subscription import MySubscription
from apps.portal.services.keys import (
    InstanceNotAllowedError,
    KeyLimitReachedError,
    MyKeys,
)


class KeyCommands(CommandSet):
    def handlers(self) -> Mapping[str, Handler]:
        return {
            "keys": self.list,
            "card": self.card,
            "show": self.show,
            "newkey": self.new_key,
            "reissue": self.reissue,
            "delete": self.delete,
            "sub": self.subscription,
        }

    @cached_property
    def my_keys(self) -> MyKeys:
        return MyKeys(self.user)

    def list(self, command: Command) -> Reply:
        keys = list(self.my_keys.all())
        new_key = Button(_("New key"), "newkey")
        title = _("My keys")
        if not keys:
            return Reply(_("You have no keys yet."), (new_key, *nav()), title=title)
        titles = instances.titles()
        lines = [
            f"{key.name} · {titles.get(key.source, '—')} · {key.get_status_display()}"
            for key in keys
        ]
        buttons = tuple(Button(key.name, f"card {key.pk}") for key in keys)
        return Reply(bullets(lines), (*buttons, new_key, *nav()), title=title)

    def card(self, command: Command) -> Reply:
        key = self._key(command.arg(0))
        if key is None:
            return _no_key()
        server = instances.titles().get(key.source, "—")
        return Reply(
            f"{server} · {key.get_status_display()}",
            (
                Button(_("Show the key"), f"show {key.pk}"),
                Button(_("Re-issue"), f"reissue {key.pk}"),
                Button(_("Delete"), f"delete {key.pk}"),
                *nav("keys"),
            ),
            title=key.name,
        )

    def show(self, command: Command) -> Reply:
        key = self._key(command.arg(0))
        if key is None:
            return _no_key()
        reply = KeyDelivery(self.my_keys, self.chat.links).reply(key, _device(command.arg(1)))
        return replace(reply, buttons=(*reply.buttons, *nav(f"card {key.pk}")), title=key.name)

    def new_key(self, command: Command) -> Reply:
        """Server, then device, then the key: each step's button carries the earlier choices."""
        title = _("New key")
        if not self.my_keys.can_get_key():
            return Reply(_("You cannot get another key now."), nav(), title=title)
        allowed = self.my_keys.instances()
        if not allowed:
            return Reply(_("Your plan has no servers yet."), nav(), title=title)
        source = command.arg(0) or (allowed[0].source if len(allowed) == 1 else "")
        if source not in {instance.source for instance in allowed}:
            return Reply(
                _("Choose a server."),
                (*(Button(i.title, f"newkey {i.source}") for i in allowed), *nav()),
                title=title,
            )
        device = _device(command.arg(1))
        if device is None:
            # With one server there is no step before this one to go back to.
            back = "newkey" if len(allowed) > 1 else ""
            return Reply(
                _("Choose the device for the key."),
                (*(Button(d.label, f"newkey {source} {d.value}") for d in Device), *nav(back)),
                title=title,
            )
        return self._issue(source, device)

    def reissue(self, command: Command) -> Reply:
        key = self._key(command.arg(0))
        if key is None:
            return _no_key()
        if command.arg(1) != "yes":
            return _confirm(
                key,
                _("Re-issue «%(name)s»? The old key stops working at once.") % {"name": key.name},
                Button(_("Re-issue"), f"reissue {key.pk} yes"),
            )
        try:
            new = self.my_keys.reissue(key)
        except KeyRevokingError:
            return _no_key()
        except (InstanceNotAllowedError, OwnerCannotHaveKeysError):
            return Reply(
                _("This key cannot be re-issued. Delete it and get a new one."),
                nav(f"card {key.pk}"),
                title=key.name,
            )
        return Reply(
            _("The old key is revoked. The new one will be ready in a moment."),
            (Button(_("Show the key"), f"show {new.pk}"), *nav("keys")),
            title=new.name,
        )

    def delete(self, command: Command) -> Reply:
        key = self._key(command.arg(0))
        if key is None:
            return _no_key()
        if command.arg(1) != "yes":
            return _confirm(
                key,
                _("Delete «%(name)s»? It stops working and cannot be restored.")
                % {"name": key.name},
                Button(_("Delete"), f"delete {key.pk} yes"),
            )
        try:
            self.my_keys.delete(key)
        except KeyRevokingError:
            return _no_key()
        return Reply(
            _("The key is deleted and stops working in a moment."),
            (Button(_("My keys"), "keys"), *nav()),
            title=key.name,
        )

    def subscription(self, command: Command) -> Reply:
        """The link, or a new one: `sub new` asks first when one exists, `sub new yes` makes it."""
        title = _("Subscription link")
        subscription = MySubscription(self.user)
        if not subscription.available:
            return Reply(_("Subscription links are turned off."), nav(), title=title)
        token = subscription.token()
        if command.arg(0) == "new":
            if token is not None and command.arg(1) != "yes":
                return Reply(
                    _("Make a new link? The current one stops working at once."),
                    (Button(_("New link"), "sub new yes"), Button(_("Cancel"), "sub")),
                    title=title,
                )
            token = subscription.renew()
        if token is None:
            return Reply(
                _("You have no subscription link yet."),
                (Button(_("Create link"), "sub new"), *nav()),
                title=title,
            )
        url = self.chat.links.to("subscription-feed", token)
        text = _(
            "Add the link to v2rayN, v2RayTun, Hiddify, Streisand or another XRay client as a "
            "subscription: it keeps your XRay keys up to date. Anyone with the link gets your "
            "keys.\n\n%(url)s"
        ) % {"url": url}
        return Reply(text, (Button(_("New link"), "sub new"), *nav()), title=title)

    def _issue(self, source: str, device: Device) -> Reply:
        title = _("New key")
        try:
            key = self.my_keys.issue(source, str(device.label))
        except (KeyLimitReachedError, OwnerCannotHaveKeysError):
            return Reply(_("You cannot get another key now."), nav(), title=title)
        except InstanceNotAllowedError:
            return Reply(_("This server is not available to you."), nav(), title=title)
        return Reply(
            _("The key is being issued. Press the button in a minute."),
            (Button(_("Show the key"), f"show {key.pk} {device.value}"), *nav("keys")),
            title=key.name,
        )

    def _key(self, value: str) -> AccessKey | None:
        """Only the user's own keys: an id of someone else's key finds nothing."""
        pk = number(value)
        return None if pk is None else self.my_keys.all().filter(pk=pk).first()


def _device(value: str) -> Device | None:
    return Device(value) if value in Device.values else None


def _confirm(key: AccessKey, question: str, yes: Button) -> Reply:
    return Reply(question, (yes, Button(_("Cancel"), f"card {key.pk}")), title=key.name)


def _no_key() -> Reply:
    return Reply(_("There is no such key."), (Button(_("My keys"), "keys"), *nav()))
