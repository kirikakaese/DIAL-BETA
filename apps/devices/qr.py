"""QR code helpers for softphone onboarding and guest-extension claiming."""
import io

import qrcode


def qr_png(data: str, box_size: int = 6) -> bytes:
    img = qrcode.make(data, box_size=box_size, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
