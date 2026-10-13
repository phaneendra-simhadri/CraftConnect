from xml.sax.saxutils import escape

import httpx

from .config import settings


def normalize_number(raw: str) -> str:
    return (raw or "").strip().lower()


def from_twilio(form: dict) -> dict:
    media_count = int(form.get("NumMedia", "0") or "0")
    return {
        "channel": "whatsapp",
        "user_id": normalize_number(form.get("From", "")),
        "text": (form.get("Body", "") or "").strip(),
        "media_url": form.get("MediaUrl0") if media_count else None,
        "media_type": form.get("MediaContentType0") if media_count else None,
    }


def twiml(messages: list[str]) -> str:
    body = "".join(f"<Message>{escape(m)}</Message>" for m in messages if m)
    return f"<?xml version='1.0' encoding='UTF-8'?><Response>{body}</Response>"


def send_whatsapp(to: str, body: str) -> bool:
    if not settings.twilio_enabled or not settings.twilio_whatsapp_from:
        return False
    try:
        url = (
            "https://api.twilio.com/2010-04-01/Accounts/"
            f"{settings.twilio_account_sid}/Messages.json"
        )
        response = httpx.post(
            url,
            auth=(settings.twilio_account_sid, settings.twilio_auth_token),
            data={"From": settings.twilio_whatsapp_from, "To": to, "Body": body},
            timeout=20,
        )
        return response.status_code < 300
    except Exception:
        return False


def absolute_buyer_url(path: str) -> str:
    if not path:
        return ""
    if settings.public_base_url:
        return f"{settings.public_base_url}{path}"
    return path
