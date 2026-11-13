"""Screenshots attached to messages: images only, told by their first bytes, not their name."""

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.utils.translation import gettext_lazy as _

MAX_IMAGE_SIZE = 5 * 1024 * 1024

# The stubs make UploadedFile generic; at runtime it is not, and `type` is evaluated lazily.
type Upload = UploadedFile[bytes]

# Magic bytes of the image formats browsers show; WebP is "RIFF....WEBP".
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def image_type(head: bytes) -> str | None:
    """The media type of an image starting with `head`, or None for anything else."""
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return next((kind for magic, kind in _SIGNATURES if head.startswith(magic)), None)


def validate_image(file: Upload) -> str:
    """The media type of an uploaded image; a file too big or not an image is refused."""
    if file.size is None or file.size > MAX_IMAGE_SIZE:
        raise ValidationError(_("The image must be at most 5 MB."))
    file.seek(0)
    kind = image_type(file.read(12))
    file.seek(0)
    if kind is None:
        raise ValidationError(_("Attach a PNG, JPEG, GIF or WebP image."))
    return kind
