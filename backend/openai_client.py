import json
import re
from typing import Any, Optional

from . import prompts
from .config import settings

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


LISTING_FIELDS = [
    "title",
    "story",
    "materials",
    "care",
    "production",
    "variations",
    "cultural_note",
    "photo_note",
    "buyer_faq",
]

_CARE_WORDS = {"wash", "washing", "washable", "dry", "clean", "cleaning", "iron", "bleach", "detergent", "dryclean"}
_CULTURAL_WORDS = {"symbol", "symbolize", "symbolizes", "symbolise", "symbolises", "meaning", "means", "heritage", "represents", "signifies", "auspicious", "prosperity", "fortune", "luck", "sacred", "ritual", "blessing"}
_MATERIAL_WORDS = {"cotton", "silk", "wool", "linen", "jute", "dye", "indigo", "thread", "fibre", "fiber", "handspun", "clay", "brass", "wood"}
_PROCESS_WORDS = {"made", "handmade", "woven", "weave", "loom", "handloom", "takes", "days", "weeks", "week", "hand", "crafted", "produced", "dyed", "spin", "deliver", "delivered", "delivery", "ships", "shipping", "dispatch", "month", "months"}
_VARIATION_WORDS = {"varies", "variation", "vary", "unique", "slight", "shade", "texture", "no two", "each piece"}
_PHOTO_WORDS = {"photo", "photograph", "picture", "exact", "one of a kind", "one-of-a-kind", "pictured"}


def _client():
    if not settings.llm_enabled or OpenAI is None:
        return None
    if settings.openai_base_url:
        return OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
    return OpenAI(api_key=settings.openai_api_key)


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def chat_json(system: str, user: str) -> dict:
    client = _client()
    if client is None:
        raise RuntimeError("llm disabled")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    try:
        response = client.chat.completions.create(
            model=settings.openai_model,
            messages=messages,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
    except Exception:
        response = client.chat.completions.create(
            model=settings.openai_model,
            messages=messages,
            temperature=0.2,
        )
    return _extract_json(response.choices[0].message.content or "{}")


def _is_gemini() -> bool:
    return "generativelanguage.googleapis.com" in (settings.openai_base_url or "")


_MIMES = {
    ".webm": "audio/webm",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".mp4": "audio/mp4",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
}


def _gemini_transcribe(data: bytes, mime: str) -> str:
    import base64
    import os as _os

    import httpx

    if not (settings.openai_api_key and _is_gemini()):
        return ""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.openai_transcribe_model}:generateContent?key={settings.openai_api_key}"
    )
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": "Transcribe this audio exactly. Reply with only the transcript text."},
                    {
                        "inline_data": {
                            "mime_type": mime or "audio/webm",
                            "data": base64.b64encode(data).decode(),
                        }
                    },
                ]
            }
        ]
    }
    try:
        response = httpx.post(url, json=payload, timeout=60)
        response.raise_for_status()
        candidates = response.json().get("candidates") or []
        parts = ((candidates[0] if candidates else {}).get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts).strip()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            print("[transcribe] Gemini quota exceeded (free tier ~20 req/day). Voice needs a paid key.")
        return ""
    except Exception:
        return ""


def transcribe(data: bytes, filename: str = "audio.webm") -> str:
    import os as _os

    mime = _MIMES.get(_os.path.splitext(filename or "")[1].lower(), "audio/webm")
    if _is_gemini():
        text = _gemini_transcribe(data, mime)
        if text:
            return text
    if settings.groq_api_key:
        text = _groq_transcribe(data, filename)
        if text:
            return text
    client = _client()
    if client is None:
        return ""
    try:
        result = client.audio.transcriptions.create(
            model=settings.openai_transcribe_model,
            file=(filename, data),
        )
        return (result.text or "").strip()
    except Exception:
        return ""


def _groq_transcribe(data: bytes, filename: str = "audio.webm") -> str:
    import httpx

    if not settings.groq_api_key:
        return ""
    try:
        response = httpx.post(
            f"{settings.groq_base_url}/audio/transcriptions",
            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
            files={"file": (filename, data)},
            data={"model": settings.groq_transcribe_model},
            timeout=60,
        )
        response.raise_for_status()
        return (response.json().get("text") or "").strip()
    except Exception:
        return ""


def extract_facts(question_type: str, question: str, answer: str) -> list[dict]:
    client = _client()
    if client is None:
        return _mock_extract(question_type, answer)
    try:
        payload = chat_json(
            prompts.EXTRACT_SYSTEM,
            prompts.extract_user(question_type, question, answer),
        )
        facts = payload.get("facts") or []
        cleaned = []
        for fact in facts:
            text = str(fact.get("text", "")).strip()
            if text:
                cleaned.append({"type": question_type, "text": text})
        return cleaned or _mock_extract(question_type, answer)
    except Exception:
        return _mock_extract(question_type, answer)


def draft_listing(facts: list[dict]) -> dict:
    client = _client()
    facts_block = _facts_block(facts)
    if client is None:
        return _mock_draft(facts)
    try:
        payload = chat_json(prompts.DRAFT_SYSTEM, prompts.draft_user(facts_block))
        return {field: str(payload.get(field, "")).strip() for field in LISTING_FIELDS}
    except Exception:
        return _mock_draft(facts)


def audit_listing(draft: dict, facts: list[dict]) -> list[dict]:
    client = _client()
    draft_block = _draft_block(draft)
    facts_block = _facts_block(facts)
    if client is None:
        return _mock_audit(draft, facts)
    try:
        payload = chat_json(
            prompts.GUARD_SYSTEM,
            prompts.guard_user(draft_block, facts_block),
        )
        claims = payload.get("claims") or []
        cleaned = []
        for claim in claims:
            cleaned.append(
                {
                    "field": str(claim.get("field", "")).strip(),
                    "claim": str(claim.get("claim", "")).strip(),
                    "type": str(claim.get("type", "general")).strip().lower(),
                    "status": str(claim.get("status", "unsupported")).strip().lower(),
                    "evidence": str(claim.get("evidence", "")).strip(),
                    "replacement": str(claim.get("replacement", "")).strip(),
                }
            )
        return cleaned or _mock_audit(draft, facts)
    except Exception:
        return _mock_audit(draft, facts)


NON_ANSWER_PHRASES = (
    "don't know",
    "do not know",
    "dont know",
    "not sure",
    "no idea",
    "skip",
    "n/a",
    "not applicable",
    "none",
    "nothing to add",
)


def is_non_answer(text: str) -> bool:
    norm = (text or "").strip().lower()
    return any(phrase in norm for phrase in NON_ANSWER_PHRASES)


def _facts_block(facts: list[dict]) -> str:
    return "\n".join(f"- [{f['type']}] {f['text']}" for f in facts) or "(none confirmed)"


def _draft_block(draft: dict) -> str:
    lines = []
    for field in LISTING_FIELDS:
        value = draft.get(field, "")
        if value:
            lines.append(f"{field}: {value}")
    return "\n".join(lines)


def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text.strip())
    return [p.strip(" -•\t") for p in parts if p and p.strip(" -•\t")]


def _classify(sentence: str) -> str:
    stripped = sentence.strip()
    if stripped.upper().startswith("Q:") or stripped.endswith("?"):
        return "neutral"
    if stripped.upper().startswith("A:"):
        stripped = stripped[2:].strip()
    tokens = _tokens(stripped)
    if tokens & _CULTURAL_WORDS:
        return "cultural"
    if tokens & _CARE_WORDS:
        return "care"
    if tokens & _PHOTO_WORDS:
        return "photo"
    if tokens & _VARIATION_WORDS:
        return "variation"
    if tokens & _MATERIAL_WORDS:
        return "material"
    if tokens & _PROCESS_WORDS:
        return "process"
    return "general"


_BARE_CONFIRM = {
    "yes", "y", "yeah", "yep", "confirm", "confirmed", "correct", "right",
    "ok", "okay", "sure", "no", "n", "nope",
}


def is_bare_confirm(text: str) -> bool:
    return re.sub(r"[^a-z]", "", (text or "").lower()) in {w for w in _BARE_CONFIRM}


def _clean_sentence(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip()).strip(" \"'`")
    if text and not text[0].isupper():
        text = text[0].upper() + text[1:]
    if text and text[-1] not in ".!?":
        text += "."
    return text


def _mock_extract(question_type: str, answer: str) -> list[dict]:
    answer = _clean_sentence(answer)
    if not answer:
        return []
    return [{"type": question_type, "text": answer}]


def _mock_draft(facts: list[dict]) -> dict:
    by_type: dict[str, list[str]] = {}
    for fact in facts:
        by_type.setdefault(fact["type"], []).append(fact["text"])

    def first(ftype: str, default: str = "") -> str:
        values = by_type.get(ftype)
        return values[0] if values else default

    title = first("identity") or first("material", "Handmade Piece")
    story = first("provenance") or first("material", "A handmade piece.")
    materials = first("material", "")
    care = first("care", "Machine wash on a gentle cycle and tumble dry low.")
    if first("care"):
        care = care.rstrip(".") + ". It is also machine washable."
    prod_bits = []
    for _t in ("process", "making_time", "delivery"):
        _v = first(_t)
        if _v and _v not in prod_bits:
            prod_bits.append(_v.rstrip("."))
    if prod_bits:
        production = ". ".join(prod_bits) + ". Ships within two days."
    else:
        production = "Each piece is made to order."
    variations = first("variation", "")
    cultural_base = first("cultural", "")
    cultural_note = (
        cultural_base.rstrip(".") + ". This traditional motif symbolises prosperity and good fortune."
    ).strip()
    photo_note = first("photo", "")

    faq_lines = []
    if first("care"):
        faq_lines.append("Q: Is this machine washable? A: " + first("care") + ".")
    if first("photo"):
        faq_lines.append("Q: Is the photo the exact item? A: " + first("photo") + ".")
    if first("process"):
        faq_lines.append("Q: Why does delivery take longer? A: " + first("process") + ".")
    if first("variation"):
        faq_lines.append("Q: Will mine look exactly like the photo? A: " + first("variation") + ".")
    buyer_faq = "\n".join(faq_lines) or "Q: Any questions? A: Please ask the maker."

    return {
        "title": title or "Handmade Piece",
        "story": story,
        "materials": materials,
        "care": care,
        "production": production,
        "variations": variations,
        "cultural_note": cultural_note,
        "photo_note": photo_note,
        "buyer_faq": buyer_faq,
    }


def _mock_audit(draft: dict, facts: list[dict]) -> list[dict]:
    facts_by_type: dict[str, list[str]] = {}
    for fact in facts:
        facts_by_type.setdefault(fact["type"], []).append(fact["text"])

    claims = []
    for field in LISTING_FIELDS:
        value = draft.get(field, "")
        if not value:
            continue
        for sentence in _split_sentences(value):
            ctype = _classify(sentence)
            supported = _has_support(ctype, sentence, facts_by_type)
            claims.append(
                {
                    "field": field,
                    "claim": sentence,
                    "type": ctype,
                    "status": "supported" if supported else "unsupported",
                    "evidence": facts_by_type.get(ctype, [""])[0] if supported else "",
                    "replacement": "",
                }
            )
    return claims


def _has_support(ctype: str, sentence: str, facts_by_type: dict[str, list[str]]) -> bool:
    if ctype == "neutral":
        return True
    candidates = facts_by_type.get(ctype, [])
    if not candidates:
        if ctype in ("general",):
            all_facts = [t for values in facts_by_type.values() for t in values]
            candidates = all_facts
        else:
            return False
    sentence_tokens = _tokens(sentence)
    for candidate in candidates:
        candidate_tokens = _tokens(candidate)
        if not candidate_tokens:
            continue
        overlap = len(sentence_tokens & candidate_tokens) / max(len(candidate_tokens), 1)
        if overlap >= 0.3:
            return True
    return False


def parse_price(text: str) -> Optional[int]:
    digits = re.sub(r"[^0-9]", "", text or "")
    return int(digits) if digits else None


_KB_CARE = {"wash", "washing", "washable", "dry", "clean", "cleanable", "launder"}
_KB_PHOTO = {"photo", "photograph", "picture", "exact", "same", "receive"}
_KB_TIME = {"long", "time", "delivery", "deliver", "ship", "shipping", "weeks", "days", "takes", "wait", "ready"}
_KB_VARY = {"vary", "varies", "variation", "differ", "difference", "unique", "identical", "same", "consistent"}
_KB_CULTURE = {"meaning", "mean", "symbol", "symbolise", "symbolize", "culture", "cultural", "tradition", "heritage", "significance"}
_KB_MATERIAL = {"material", "fabric", "made", "cotton", "silk", "wool", "dye", "colour", "color", "thread"}
_KB_DELIVERY = {"deliver", "delivered", "delivery", "arrive", "arrives", "arrival", "shipping", "ship", "ships", "dispatch", "courier", "receive"}
_KB_MAKING = {"make", "makes", "making", "produce", "produced", "production", "long", "weeks", "week", "days", "day", "takes", "take", "craft"}
_KB_REQUEST_PHRASES = (
    "can you",
    "could you",
    "would you",
    "do you",
    "make it",
    "make this",
    "custom",
    "customi",
    "available in",
    "come in",
    "other colour",
    "other color",
    "different colour",
    "different color",
    "another colour",
    "another color",
)


def mock_kb_answer(question: str, facts: list[dict], qa_history: list[dict]) -> Optional[str]:
    ql = (question or "").lower()
    qt = _tokens(question)
    for item in reversed(qa_history):
        if item.get("status") == "answered" and item.get("answer"):
            base = _tokens(item.get("question", ""))
            if base and len(base & qt) / max(len(base), 1) >= 0.5:
                return item["answer"]
    if any(phrase in ql for phrase in _KB_REQUEST_PHRASES):
        return None
    by: dict[str, list[str]] = {}
    for fact in facts:
        by.setdefault(fact["type"], []).append(fact["text"])

    def first(ftype: str) -> Optional[str]:
        values = by.get(ftype)
        return values[0] if values else None

    checks = [
        (_KB_CARE, "care"),
        (_KB_DELIVERY, "delivery"),
        (_KB_MAKING, "making_time"),
        (_KB_PHOTO, "photo"),
        (_KB_TIME, "process"),
        (_KB_VARY, "variation"),
        (_KB_CULTURE, "cultural"),
        (_KB_MATERIAL, "material"),
    ]
    for words, ftype in checks:
        if qt & words and first(ftype):
            return first(ftype)
    return None


def kb_answer(question: str, facts: list[dict], qa_history: list[dict]) -> Optional[str]:
    client = _client()
    if client is None:
        return mock_kb_answer(question, facts, qa_history)
    try:
        prior = (
            "\n".join(f"Q: {i['question']}\nA: {i['answer']}" for i in qa_history if i.get("answer"))
            or "(none)"
        )
        payload = chat_json(
            prompts.KB_SYSTEM,
            f"Confirmed facts:\n{_facts_block(facts)}\n\nPreviously answered:\n{prior}\n\n"
            f"Buyer question: {question}\n\nAnswer.",
        )
        answer = str(payload.get("answer", "")).strip()
        if payload.get("answerable") and answer:
            return answer
        return None
    except Exception:
        return mock_kb_answer(question, facts, qa_history)


def translate_to_english(text: str) -> tuple[str, str]:
    client = _client()
    if client is None or not (text or "").strip():
        return text, "English"
    try:
        payload = chat_json(prompts.TRANSLATE_TO_EN_SYSTEM, text)
        english = str(payload.get("english") or text).strip()
        language = str(payload.get("language") or "English").strip()
        return english, language
    except Exception:
        return text, "English"


def translate_from_english(text: str, language: str) -> str:
    if not text or not language or language.lower() == "english":
        return text
    client = _client()
    if client is None:
        return text
    try:
        payload = chat_json(
            prompts.TRANSLATE_FROM_EN_SYSTEM,
            f"Target language: {language}\n\nText:\n{text}",
        )
        return str(payload.get("text") or text).strip()
    except Exception:
        return text
