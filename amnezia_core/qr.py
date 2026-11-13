"""QR codes for web pages (inline SVG) and for messengers (PNG)."""

import io

import segno

# Pixels per module of a PNG: big enough for a phone camera to read it off another screen.
_PNG_SCALE = 8


def qr_svg(text: str) -> str:
    """SVG markup of a QR code; error correction starts at LOW as in the app."""
    return str(segno.make_qr(text, error="l").svg_inline(scale=4))


def qr_png(text: str) -> bytes:
    """The same QR code as a PNG image, for chats that show pictures but not SVG."""
    buffer = io.BytesIO()
    segno.make_qr(text, error="l").save(buffer, kind="png", scale=_PNG_SCALE, border=4)
    return buffer.getvalue()
