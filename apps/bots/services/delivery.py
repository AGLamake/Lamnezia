"""Giving a key out in a chat, under the reveal policy (DESIGN.md 6.5) and `bot_key_delivery`."""

import re

from django.utils.translation import gettext as _

from apps.bots.services.replies import Attachment, Button, Reply
from apps.bots.services.session import SiteLinks
from apps.core.models import BotKeyDelivery
from apps.core.services.site_settings import cached_settings
from apps.guides.services.devices import Device
from apps.keys.models import AccessKey, KeyStatus
from apps.portal.services.keys import MyKeys

_UNSAFE_IN_FILE_NAME = re.compile(r"[^A-Za-z0-9_-]+")


class KeyDelivery:
    """What the bot answers when the user asks to see one of their keys.

    Under `link_only` the key never goes to the chat and is not counted as shown: the user
    opens it on the portal. Otherwise the key is revealed exactly as on the portal, so the
    "once" policy is spent by whichever of the two shows it first.
    """

    def __init__(self, my_keys: MyKeys, links: SiteLinks) -> None:
        self._my_keys = my_keys
        self._links = links

    def reply(self, key: AccessKey, device: Device | None = None) -> Reply:
        if key.status == KeyStatus.ISSUING:
            return self._issuing(key, device)
        if cached_settings.get().bot_key_delivery == BotKeyDelivery.LINK_ONLY:
            return Reply(
                _("Open the key on the panel: the bot does not send keys to chats."),
                (Button(_("Open the key"), url=self._links.to("portal:key", key.pk)),),
            )
        export = self._my_keys.reveal(key)
        if export is None:
            return Reply(
                _("This key cannot be shown again. Re-issue it to get a new one."),
                (Button(_("Re-issue"), f"reissue {key.pk}"),),
            )
        files = tuple(
            Attachment(
                _file_name(key.name, file.extension), file.content.encode(), file.content_type
            )
            for file in export.files
        )
        pictures = tuple(
            Attachment(f"qr-{number}.png", image, "image/png")
            for number, image in enumerate(export.qr_images(), start=1)
        )
        text = (
            _(
                "Key «%(name)s». Add it in the Amnezia app: paste the vpn:// link, scan the "
                "QR codes in order or open the %(files)s file."
            )
            if files
            else _(
                "Key «%(name)s». Add it in the Amnezia app: paste the vpn:// link or scan the "
                "QR codes in order."
            )
        )
        names = ", ".join(f".{file.extension}" for file in export.files)
        return Reply(
            text % {"name": key.name, "files": names},
            (self._app_button(device),),
            code=export.vpn_link,
            pictures=pictures,
            files=files,
            private=True,
        )

    def _issuing(self, key: AccessKey, device: Device | None) -> Reply:
        if self._my_keys.issue_failed(key):
            return Reply(
                _("The key could not be issued. Delete it and get a new one."),
                (Button(_("Delete"), f"delete {key.pk}"),),
            )
        action = f"show {key.pk} {device.value}" if device else f"show {key.pk}"
        return Reply(
            _("The key is being issued. Press the button in a minute."),
            (Button(_("Show the key"), action),),
        )

    def _app_button(self, device: Device | None) -> Button:
        if device is None:
            return Button(_("Get the Amnezia app"), url=self._links.to("guides:index"))
        return Button(
            _("Amnezia app for %(device)s") % {"device": device.label},
            url=self._links.to("guides:platform", device.value),
        )


def _file_name(key_name: str, extension: str) -> str:
    return f"{_UNSAFE_IN_FILE_NAME.sub('_', key_name).strip('_') or 'amnezia'}.{extension}"
