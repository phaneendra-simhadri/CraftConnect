#!/usr/bin/env python3
"""Source-Truth Listings - single-file app.

Run it:
    python app.py

It installs missing dependencies, starts the server, and opens the browser.
Set OPENAI_API_KEY in a .env file (or environment) to use real AI; otherwise it
runs fully offline with a deterministic mock.
"""

import importlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from urllib.parse import quote
from xml.sax.saxutils import escape

DEPENDENCIES = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn[standard]"),
    ("openai", "openai"),
    ("dotenv", "python-dotenv"),
    ("pydantic", "pydantic"),
    ("multipart", "python-multipart"),
    ("httpx", "httpx"),
]


def _ensure_dependencies():
    missing = []
    for module, package in DEPENDENCIES:
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(package)
    if missing:
        print(f"Installing missing packages: {', '.join(missing)}")
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])


_ensure_dependencies()

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

import httpx  # noqa: E402
from fastapi import FastAPI, File, Form, Request, UploadFile  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, Response  # noqa: E402

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
def _bool(value, default=False):
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
GROQ_TRANSCRIBE_MODEL = os.getenv("GROQ_TRANSCRIBE_MODEL", "whisper-large-v3")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
USE_MOCK_LLM = _bool(os.getenv("USE_MOCK_LLM"), False)
LLM_ENABLED = bool(OPENAI_API_KEY) and not USE_MOCK_LLM


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------
EXTRACT_SYSTEM = """You convert an artisan's spoken answer into confirmed facts for a product listing.
Return JSON only, shaped as {"facts": [{"type": "...", "text": "..."}]}.
Allowed types: identity, colour, material, care, process, making_time, delivery, variation, photo, cultural, price, provenance, general.
Rewrite each fact as one short, clear, grammatically correct English sentence. Fix grammar and
spelling, but keep the artisan's exact meaning and their own terms. Never infer, embellish, or add
any detail, number, material, or claim they did not give. Do not translate away proper nouns.
If the answer contains nothing factual, return {"facts": []}."""

DRAFT_SYSTEM = """You are an expert marketplace copywriter for handmade crafts.
Write a complete, warm, trust-building product listing that helps the piece sell and
reduces buyer questions. Return JSON only with these string fields:
title, story, materials, care, production, variations, cultural_note, photo_note, buyer_faq.
- story: 2-3 sentences about the maker and the craft.
- materials: what it is made from.
- care: how to care for it.
- production: how it is made and why handmade takes longer.
- variations: what naturally differs between pieces.
- cultural_note: the meaning or significance of the pattern, if any.
- photo_note: whether the photo shows the exact piece and whether it is one of a kind.
- buyer_faq: 3-5 short Q&A lines answering common buyer questions.
Write naturally and completely."""

GUARD_SYSTEM = """You are a strict claim auditor for handmade crafts.
You receive a draft listing and a ledger of facts the maker explicitly confirmed.
Break every text field into atomic claims and audit each one. Return JSON only:
{"claims": [{"field": "...", "claim": "...", "type": "...", "status": "...", "evidence": "...", "replacement": "..."}]}

Rules:
- type is one of: material, care, cultural, process, variation, photo, general, neutral.
- status is one of: supported, unsupported, cultural_unverified.
- "supported" ONLY when the ledger explicitly states it. Put the matching ledger fact in evidence.
- Cultural claims about meaning/symbolism are "cultural_unverified" unless the ledger has an
  explicitly confirmed cultural fact. Never allow an invented heritage claim.
- Care claims are "unsupported" unless the ledger explicitly confirms care.
- "neutral" is for non-factual pleasantries (e.g. "Thank you for supporting handmade").
- Be conservative. When unsure, choose unsupported.
- For unsupported/cultural_unverified claims, put a safe replacement in replacement, or "" to drop it.
Split into the smallest meaningful claims. Use the exact field names from the draft."""

TRANSLATE_TO_EN_SYSTEM = """Detect the language of the text and translate it to English.
Return JSON only: {"language": "<English name of the detected language>", "english": "<the text in English>"}.
If the text is already English, return it unchanged with language "English".
Translate meaning faithfully and do not add or remove information."""

TRANSLATE_FROM_EN_SYSTEM = """Translate the given English text into the requested target language.
Return JSON only: {"text": "<translation>"}. Keep any *asterisks*, numbers, and URLs intact."""

KB_SYSTEM = """You are a shop assistant answering a buyer's question about a handmade product.
You are given the product's confirmed facts and previously answered questions. That is your knowledge base.
Answer from that knowledge base whenever it covers the question, even if the buyer words it differently
(paraphrases, synonyms, or indirect phrasings all count). Reuse the meaning of the facts and prior answers.
Be generous about matching: if a confirmed fact answers the intent of the question, answer it.
Only set answerable to false when the knowledge base genuinely has nothing relevant at all
(e.g. a customisation request like a different colour, or a fact that was never captured).
Return JSON only: {"answerable": true/false, "answer": "..."}.
Never guess, invent, or add details that are not in the knowledge base.
If answerable, write a short, warm, direct answer in one or two sentences using only the knowledge base.
If the question asks about cultural meaning and the facts do not document it, it is NOT answerable."""

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


# --------------------------------------------------------------------------
# LLM client (with offline mock)
# --------------------------------------------------------------------------
_CARE_WORDS = {"wash", "washing", "washable", "dry", "clean", "cleaning", "iron", "bleach", "detergent", "dryclean"}
_CULTURAL_WORDS = {"symbol", "symbolize", "symbolizes", "symbolise", "symbolises", "meaning", "means", "heritage", "represents", "signifies", "auspicious", "prosperity", "fortune", "luck", "sacred", "ritual", "blessing"}
_MATERIAL_WORDS = {"cotton", "silk", "wool", "linen", "jute", "dye", "indigo", "thread", "fibre", "fiber", "handspun", "clay", "brass", "wood"}
_PROCESS_WORDS = {"made", "handmade", "woven", "weave", "loom", "handloom", "takes", "days", "weeks", "week", "hand", "crafted", "produced", "dyed", "spin", "deliver", "delivered", "delivery", "ships", "shipping", "dispatch", "month", "months"}
_VARIATION_WORDS = {"varies", "variation", "vary", "unique", "slight", "shade", "texture", "no two", "each piece"}
_PHOTO_WORDS = {"photo", "photograph", "picture", "exact", "one of a kind", "one-of-a-kind", "pictured"}
NON_ANSWER_PHRASES = ("don't know", "do not know", "dont know", "not sure", "no idea", "skip", "n/a", "not applicable", "none", "nothing to add")


def _client():
    if not LLM_ENABLED or OpenAI is None:
        return None
    if OPENAI_BASE_URL:
        return OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
    return OpenAI(api_key=OPENAI_API_KEY)


def _extract_json(text):
    text = (text or "").strip()
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


def _chat_json(system, user):
    client = _client()
    if client is None:
        raise RuntimeError("llm disabled")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
    except Exception:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.2,
        )
    return _extract_json(response.choices[0].message.content or "{}")


def _facts_block(facts):
    return "\n".join(f"- [{f['type']}] {f['text']}" for f in facts) or "(none confirmed)"


def _draft_block(draft):
    return "\n".join(f"{f}: {draft.get(f, '')}" for f in LISTING_FIELDS if draft.get(f))


def _tokens(text):
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _split_sentences(text):
    parts = re.split(r"(?<=[.!?])\s+|\n+", (text or "").strip())
    return [p.strip(" -\t") for p in parts if p and p.strip(" -\t")]


def _classify(sentence):
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


def is_non_answer(text):
    norm = (text or "").strip().lower()
    return any(phrase in norm for phrase in NON_ANSWER_PHRASES)


def _is_gemini():
    return "generativelanguage.googleapis.com" in OPENAI_BASE_URL


def _gemini_transcribe(data, mime="audio/webm"):
    import base64

    if not (OPENAI_API_KEY and _is_gemini()):
        return ""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{OPENAI_TRANSCRIBE_MODEL}:generateContent?key={OPENAI_API_KEY}"
    )
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": "Transcribe this audio exactly. Reply with only the transcript text."},
                    {"inline_data": {"mime_type": mime or "audio/webm", "data": base64.b64encode(data).decode()}},
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


def transcribe(data, filename="audio.webm"):
    _mimes = {
        ".webm": "audio/webm",
        ".ogg": "audio/ogg",
        ".oga": "audio/ogg",
        ".mp3": "audio/mpeg",
        ".mp4": "audio/mp4",
        ".m4a": "audio/mp4",
        ".wav": "audio/wav",
    }
    mime = _mimes.get(os.path.splitext(filename or "")[1].lower(), "audio/webm")
    if _is_gemini():
        text = _gemini_transcribe(data, mime)
        if text:
            return text
    if GROQ_API_KEY:
        text = _groq_transcribe(data, filename)
        if text:
            return text
    client = _client()
    if client is None:
        return ""
    try:
        result = client.audio.transcriptions.create(model=OPENAI_TRANSCRIBE_MODEL, file=(filename, data))
        return (result.text or "").strip()
    except Exception:
        return ""


def _groq_transcribe(data, filename="audio.webm"):
    if not GROQ_API_KEY:
        return ""
    try:
        response = httpx.post(
            f"{GROQ_BASE_URL}/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": (filename, data)},
            data={"model": GROQ_TRANSCRIBE_MODEL},
            timeout=60,
        )
        response.raise_for_status()
        return (response.json().get("text") or "").strip()
    except Exception:
        return ""


def _clean_sentence(text):
    import re as _re

    text = (text or "").strip()
    text = _re.sub(r"\s+", " ", text)
    text = text.strip(" \"'`")
    if text and not text[0].isupper():
        text = text[0].upper() + text[1:]
    if text and text[-1] not in ".!?":
        text += "."
    return text


def extract_facts(question_type, question, answer):
    client = _client()
    if client is None:
        return _mock_extract(question_type, answer)
    try:
        payload = _chat_json(
            EXTRACT_SYSTEM,
            f"Question topic: {question_type}\nQuestion asked: {question}\nArtisan answer: {answer}\n\n"
            "Extract the minimal set of facts. Rewrite each as one short, grammatically correct English "
            "sentence that preserves the artisan's exact meaning and adds nothing.",
        )
        cleaned = []
        for fact in payload.get("facts") or []:
            text = _clean_sentence(fact.get("text", ""))
            if text:
                cleaned.append({"type": question_type, "text": text})
        return cleaned or _mock_extract(question_type, answer)
    except Exception:
        return _mock_extract(question_type, answer)


def draft_listing(facts):
    client = _client()
    if client is None:
        return _mock_draft(facts)
    try:
        payload = _chat_json(
            DRAFT_SYSTEM,
            f"Confirmed maker facts:\n{_facts_block(facts)}\n\nWrite the listing now.",
        )
        return {field: str(payload.get(field, "")).strip() for field in LISTING_FIELDS}
    except Exception:
        return _mock_draft(facts)


def audit_listing(draft, facts):
    client = _client()
    if client is None:
        return _mock_audit(draft, facts)
    try:
        payload = _chat_json(
            GUARD_SYSTEM,
            f"Confirmed maker facts (the only source of truth):\n{_facts_block(facts)}\n\n"
            f"Draft listing to audit:\n{_draft_block(draft)}\n\nReturn the audited claims.",
        )
        cleaned = []
        for claim in payload.get("claims") or []:
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


def _mock_extract(question_type, answer):
    answer = _clean_sentence(answer)
    if not answer:
        return []
    return [{"type": question_type, "text": answer}]


def _mock_draft(facts):
    by_type = {}
    for fact in facts:
        by_type.setdefault(fact["type"], []).append(fact["text"])

    def first(ftype, default=""):
        return by_type.get(ftype, [default])[0]

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
    cultural_note = (cultural_base.rstrip(".") + ". This traditional motif symbolises prosperity and good fortune.").strip()
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


def _has_support(ctype, sentence, facts_by_type):
    if ctype == "neutral":
        return True
    candidates = facts_by_type.get(ctype, [])
    if not candidates:
        if ctype == "general":
            candidates = [t for values in facts_by_type.values() for t in values]
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


def _mock_audit(draft, facts):
    facts_by_type = {}
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


# --------------------------------------------------------------------------
# Buyer knowledge-base answering
# --------------------------------------------------------------------------
_KB_PRICE = {"price", "cost", "much", "howmuch", "rupees", "rs", "inr", "rate", "charge", "pay", "pricing", "expensive", "cheap", "dollar", "money", "worth", "priced"}
_KB_COLOUR = {"colour", "color", "shade", "hue", "tone"}
_KB_CARE = {"wash", "washing", "washable", "dry", "clean", "cleanable", "launder", "care"}
_KB_PHOTO = {"photo", "photograph", "picture", "exact", "same", "receive", "receive"}
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


def mock_kb_answer(question, facts, qa_history):
    hit = _keyword_kb_answer(question, facts, qa_history)
    if hit:
        return hit
    # fuzzy: answer only on strong overlap (>=0.7) to avoid mis-matches
    qt = _tokens(question)
    best = None
    best_score = 0.0
    for fact in facts:
        ftokens = _tokens(fact["text"])
        if not ftokens:
            continue
        score = len(ftokens & qt) / max(len(ftokens), 1)
        if score > best_score:
            best_score = score
            best = fact["text"]
    if best and best_score >= 0.7:
        return best
    return None


def _keyword_kb_answer(question, facts, qa_history):
    """Deterministic high-confidence match (used first so common questions never misfire)."""
    qt = _tokens(question)
    # 1. a previously answered, genuinely similar question wins (learned knowledge)
    for item in reversed(qa_history):
        if item.get("status") == "answered" and item.get("answer"):
            base = _tokens(item.get("question", ""))
            if base and len(base & qt) / max(len(base), 1) >= 0.6:
                return item["answer"]
    # 2. customisation-style requests with no learned answer must reach the seller
    if any(phrase in (question or "").lower() for phrase in _KB_REQUEST_PHRASES):
        return None
    by = {}
    for fact in facts:
        by.setdefault(fact["type"], []).append(fact["text"])

    def first(ftype):
        values = by.get(ftype)
        return values[0] if values else None

    checks = [
        (_KB_PRICE, "price"),
        (_KB_CARE, "care"),
        (_KB_COLOUR, "colour"),
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


def kb_answer(question, facts, qa_history):
    # 1. deterministic match (learned answers + common buyer questions)
    kw = _keyword_kb_answer(question, facts, qa_history)
    if kw:
        return kw
    # 2. customisation-style requests reach the seller rather than being guessed by the LLM
    if any(phrase in (question or "").lower() for phrase in _KB_REQUEST_PHRASES):
        return None
    client = _client()
    if client is None:
        return mock_kb_answer(question, facts, qa_history)
    try:
        prior = "\n".join(f"Q: {i['question']}\nA: {i['answer']}" for i in qa_history if i.get("answer")) or "(none)"
        payload = _chat_json(
            KB_SYSTEM,
            f"Confirmed facts:\n{_facts_block(facts)}\n\nPreviously answered:\n{prior}\n\n"
            f"Buyer question: {question}\n\nAnswer.",
        )
        answer = str(payload.get("answer", "")).strip()
        if payload.get("answerable") and answer:
            return answer
        return None
    except Exception:
        return mock_kb_answer(question, facts, qa_history)


def translate_to_english(text):
    client = _client()
    if client is None or not text.strip():
        return text, "English"
    try:
        payload = _chat_json(TRANSLATE_TO_EN_SYSTEM, text)
        english = str(payload.get("english") or text).strip()
        language = str(payload.get("language") or "English").strip()
        return english, language
    except Exception:
        return text, "English"


def translate_from_english(text, language):
    if not text or not language or language.lower() == "english":
        return text
    client = _client()
    if client is None:
        return text
    try:
        payload = _chat_json(
            TRANSLATE_FROM_EN_SYSTEM,
            f"Target language: {language}\n\nText:\n{text}",
        )
        return str(payload.get("text") or text).strip()
    except Exception:
        return text


def _parse_price(text):
    import re as _re

    digits = _re.sub(r"[^0-9]", "", text or "")
    return int(digits) if digits else None


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------
class Ledger:
    def __init__(self):
        self._facts = []
        self._counter = 0

    def add(self, fact_type, text, source_turn=0):
        text = (text or "").strip()
        if not text:
            return {}
        existing = {(f["type"], f["text"].lower()) for f in self._facts}
        if (fact_type, text.lower()) in existing:
            return {}
        self._counter += 1
        self._facts.append({"id": self._counter, "type": fact_type, "text": text, "source_turn": source_turn, "confirmed": True})
        return self._facts[-1]

    def add_many(self, facts, source_turn=0):
        added = []
        for fact in facts:
            item = self.add(fact.get("type", "general"), fact.get("text", ""), source_turn)
            if item:
                added.append(item)
        return added

    def all(self):
        return list(self._facts)

    def to_facts(self):
        return [{"type": f["type"], "text": f["text"]} for f in self._facts]

    def is_empty(self):
        return not self._facts


# --------------------------------------------------------------------------
# Interview
# --------------------------------------------------------------------------
QUESTIONS = [
    {"key": "identity", "required": True, "question": "Let's build your listing. What is this piece called, and what is it?", "quick_replies": ["It's a handwoven shawl"]},
    {"key": "colour", "required": True, "question": "What colour(s) is it? Describe the main colours.", "quick_replies": ["Indigo blue and off-white"]},
    {"key": "material", "required": True, "question": "What is it made from? Tell me the materials and dyes.", "quick_replies": ["Handspun cotton with natural indigo dye"]},
    {"key": "making_time", "required": True, "question": "Roughly how long does one piece take to make?", "quick_replies": ["About two weeks per piece"]},
    {"key": "delivery", "required": True, "question": "After an order, roughly how long until it is delivered?", "quick_replies": ["Made to order; ships in about 3-4 weeks"]},
    {"key": "care", "required": True, "question": "How should a buyer care for it? Say exactly what is safe (and unsafe).", "quick_replies": ["Hand wash cold, dry in shade, never machine wash"]},
    {"key": "process", "required": False, "question": "How is it made, step by step?", "quick_replies": ["Handwoven on a pit loom"]},
    {"key": "variation", "required": False, "question": "What naturally varies from piece to piece?", "quick_replies": ["Dye shade and weave texture vary slightly"]},
    {"key": "photo", "required": True, "question": "Is the photo the exact piece the buyer receives? Is it one of a kind?", "quick_replies": ["Yes, the photo is the exact piece and it's one of a kind"]},
    {"key": "cultural", "required": True, "question": "Does the pattern have a cultural meaning? Share only what is truly known.", "quick_replies": ["It is a family motif; I won't describe meaning I can't confirm"]},
    {"key": "price", "required": True, "question": "What is the price of this piece?", "quick_replies": ["1200"]},
]

REQUIRED_KEYS = [q["key"] for q in QUESTIONS if q.get("required")]
OPTIONAL_KEYS = [q["key"] for q in QUESTIONS if not q.get("required")]

CONFIRM_WORDS = {"yes", "y", "yeah", "yep", "confirm", "confirmed", "correct", "right", "ok", "okay", "sure"}
DENY_WORDS = {"no", "n", "nope", "wrong", "incorrect", "edit", "change"}
PUBLISH_WORDS = {"publish", "done", "finish", "share", "ready"}
RETRY_PHRASES = {"no", "n", "nope", "wrong", "incorrect", "edit", "change", "no, let me fix it", "let me fix it", "fix it"}
NEW_LISTING_PHRASES = {"new listing", "start over", "restart", "start a new listing", "new piece", "another listing"}


def _normalize(text):
    return (text or "").strip().lower()


def is_confirm(text):
    norm = _normalize(text)
    words = set(norm.replace("'", " ").split())
    if norm in CONFIRM_WORDS:
        return True
    return bool(words & CONFIRM_WORDS) and not bool(words & DENY_WORDS)


def is_bare_confirm(text):
    stripped = "".join(ch for ch in _normalize(text) if ch.isalpha())
    return stripped in {w for w in CONFIRM_WORDS | DENY_WORDS}


def is_retry(text):
    return _normalize(text).rstrip(".! ") in RETRY_PHRASES


def is_publish(text):
    return bool(set(_normalize(text).replace("'", " ").split()) & PUBLISH_WORDS)


def is_new_listing(text):
    norm = _normalize(text).rstrip(".! ")
    if norm in {"new", "start", "again", "restart"}:
        return True
    return any(phrase in norm for phrase in NEW_LISTING_PHRASES)


# --------------------------------------------------------------------------
# Guard
# --------------------------------------------------------------------------
CARE_FALLBACK = "Care for this piece is not yet confirmed - ask the maker."
CULTURAL_FALLBACK = "This pattern holds significance in the maker's community; its detailed meaning is not documented here."
ALLOWED_STATUS = {"supported", "unsupported", "cultural_unverified"}


def _normalize_status(status):
    status = (status or "").strip().lower()
    return status if status in ALLOWED_STATUS else "unsupported"


def _enforce(claims, ledger_facts):
    ledger_types = {f["type"] for f in ledger_facts}
    for claim in claims:
        claim["type"] = (claim.get("type") or "general").strip().lower()
        claim["status"] = _normalize_status(claim.get("status"))
        if claim["type"] == "care":
            if "care" not in ledger_types:
                claim["status"] = "unsupported"
                claim["replacement"] = CARE_FALLBACK
            elif claim["status"] != "supported":
                claim["replacement"] = ""
        elif claim["type"] == "cultural":
            if "cultural" not in ledger_types:
                claim["status"] = "cultural_unverified"
                claim["replacement"] = CULTURAL_FALLBACK
            elif claim["status"] != "supported":
                claim["replacement"] = ""
        if claim["status"] != "supported":
            claim["evidence"] = ""
    return claims


def _reassemble(draft, claims):
    grouped = {}
    for claim in claims:
        field = claim.get("field") or "story"
        if field not in LISTING_FIELDS:
            field = "story"
        text = claim.get("claim", "").strip()
        kept = text if claim["status"] in ("supported", "neutral") else claim.get("replacement", "").strip()
        if not kept:
            continue
        grouped.setdefault(field, [])
        if kept not in grouped[field]:
            grouped[field].append(kept)
    safe = {field: " ".join(grouped.get(field, [])).strip() for field in LISTING_FIELDS}
    safe["title"] = (draft.get("title") or safe.get("title") or "Handmade piece").strip()
    return safe


def guard_audit(draft, ledger_facts):
    claims = _enforce(audit_listing(draft, ledger_facts), ledger_facts)
    safe = _reassemble(draft, claims)
    ledger_types = {f["type"] for f in ledger_facts}
    if "care" not in ledger_types and not safe.get("care"):
        safe["care"] = CARE_FALLBACK
    if "cultural" not in ledger_types and not safe.get("cultural_note"):
        safe["cultural_note"] = CULTURAL_FALLBACK
    removed = [c for c in claims if c["status"] != "supported" and c["type"] != "neutral" and c.get("field") != "title"]
    return {"draft": draft, "safe": safe, "claims": claims, "removed": removed}


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------
class Session:
    def __init__(self, sid):
        self.id = sid
        self.ledger = Ledger()
        self.stage = "idle"
        self.q_index = 0
        self.pending_facts = []
        self.audit = None
        self.published = False
        self.photo = None
        self.seller_phone = None
        self.price = None
        self.qa = []
        self.language_name = "English"
        self.created = time.time()


class SessionStore:
    def __init__(self):
        self._sessions = {}

    def get(self, sid=None):
        if sid and sid in self._sessions:
            return self._sessions[sid]
        sid = sid or uuid.uuid4().hex[:10]
        session = Session(sid)
        self._sessions[sid] = session
        return session

    def remove(self, sid):
        self._sessions.pop(sid, None)

    def all(self):
        return list(self._sessions.values())


store = SessionStore()

BUYER_QUESTIONS = {}
PENDING_BY_SELLER = {}
ORDERS = {}


def publish_product(session):
    session.published = True
    if not session.seller_phone:
        session.seller_phone = session.id
    return session.seller_phone


def ask_buyer_question(session, question, buyer_id):
    answer = kb_answer(question, session.ledger.to_facts(), session.qa)
    entry = {
        "id": uuid.uuid4().hex[:10],
        "question": question.strip(),
        "answer": answer or "",
        "status": "answered" if answer else "pending",
        "buyer_id": buyer_id or "guest",
        "created": time.time(),
    }
    session.qa.append(entry)
    if not answer:
        BUYER_QUESTIONS[entry["id"]] = {"product_id": session.id, "entry": entry}
        seller = _seller_key(session)
        PENDING_BY_SELLER.setdefault(seller, []).append(entry["id"])
        if str(seller).startswith("whatsapp:"):
            title = (session.audit or {}).get("safe", {}).get("title", "your product")
            send_whatsapp(
                seller,
                f"A buyer asked about '{title}':\n\"{entry['question']}\"\n\nReply with the answer (text or voice note).",
            )
    return entry


def answer_seller_question(seller_key, text):
    pending = PENDING_BY_SELLER.get(_normalize_phone(seller_key)) or []
    if not pending:
        return None
    qid = pending[0]
    record = BUYER_QUESTIONS.get(qid)
    if not record:
        pending.pop(0)
        return None
    session = store.get(record["product_id"])
    entry = record["entry"]
    answer = (text or "").strip()
    if not answer:
        return None
    answer, _language = translate_to_english(answer)
    facts = extract_facts("buyer_question", entry["question"], answer)
    if not facts:
        facts = [{"type": "general", "text": answer}]
    session.ledger.add_many(facts)
    entry["answer"] = answer
    entry["status"] = "answered"
    entry["answered"] = time.time()
    pending.pop(0)
    BUYER_QUESTIONS.pop(qid, None)
    return entry


# --------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------
INTRO = "Hi! I turn your craft knowledge into a buyer-ready listing where nothing is invented. Every claim stays traceable to you."

SAMPLE_FACTS = [
    {"type": "identity", "text": "Handwoven indigo shawl"},
    {"type": "colour", "text": "Indigo blue and off-white"},
    {"type": "material", "text": "Handspun cotton with natural indigo dye"},
    {"type": "making_time", "text": "About two weeks per piece"},
    {"type": "delivery", "text": "Made to order; ships in about 3-4 weeks"},
    {"type": "care", "text": "Hand wash cold, dry in shade, never machine wash"},
    {"type": "process", "text": "Handwoven on a pit loom"},
    {"type": "variation", "text": "Dye shade and weave texture vary slightly"},
    {"type": "photo", "text": "The photo shows the exact piece the buyer receives; it is one of a kind"},
    {"type": "cultural", "text": "A family motif; the meaning is not documented here"},
    {"type": "price", "text": "1200"},
]


def _reply(messages, quick_replies=None, listing=None, stage=None, buyer_path=None):
    if isinstance(messages, str):
        messages = [messages]
    return {"messages": messages, "quick_replies": quick_replies or [], "listing": listing, "stage": stage, "buyer_path": buyer_path}


def _current_question(session):
    return QUESTIONS[session.q_index]


def _ask_question(session):
    question = _current_question(session)
    session.stage = "interview"
    return _reply(question["question"], question["quick_replies"], stage=session.stage)


def _ask_confirm(session):
    lines = "\n".join(f"- {f['text']}" for f in session.pending_facts)
    question = _current_question(session)
    message = f"Got it. I'll record this as *{question['key']}*:\n{lines}\n\nIs that correct?"
    session.stage = "confirm"
    return _reply(message, ["Yes", "No, let me fix it"], stage=session.stage)


def _extract(session, text):
    question = _current_question(session)
    return extract_facts(question["key"], question["question"], text)


def _advance(session):
    session.q_index += 1
    if session.q_index >= len(QUESTIONS):
        if not session.photo:
            session.stage = "photo"
            return _reply(
                "Last step: send me a *photo* of this exact piece so I can put it on your shop page "
                "(tap the camera icon here, or send the image on WhatsApp).",
                [],
                stage="photo",
            )
        return _listing_reply(session, prefix="All facts confirmed.")
    return _ask_question(session)


def _run_audit(session):
    facts = session.ledger.to_facts()
    session.audit = guard_audit(draft_listing(facts), facts)
    return session.audit


def _listing_payload(session):
    audit = session.audit
    return {
        "safe": audit["safe"],
        "removed": audit["removed"],
        "claims": audit["claims"],
        "published": session.published,
        "buyer_path": f"/buyer/{quote(session.id)}",
    }


def _listing_reply(session, prefix=""):
    audit = _run_audit(session)
    safe = audit["safe"]
    removed = audit["removed"]
    lines = [prefix] if prefix else []
    lines.append("Here is your buyer-ready listing, every claim traced to you:\n")
    lines.append(f"*{safe.get('title') or 'Your piece'}*\n")
    labels = [
        ("story", "Story"),
        ("materials", "Materials"),
        ("care", "Care"),
        ("production", "Production time"),
        ("variations", "Natural variations"),
        ("cultural_note", "Cultural note"),
        ("photo_note", "The exact piece"),
    ]
    for field, label in labels:
        if safe.get(field):
            lines.append(f"*{label}:* {safe[field]}")
    if safe.get("buyer_faq"):
        lines.append(f"*Buyer FAQ:* {safe['buyer_faq']}")
    if removed:
        lines.append(f"\nI flagged or removed {len(removed)} claim(s) I could not verify against your facts:")
        for claim in removed:
            lines.append(f"- ({claim['type']}) {claim['claim']}")
    lines.append("\nReply *Publish* to create the buyer link, or send a correction.")
    session.stage = "review"
    return _reply(lines, ["Publish", "Fix something"], listing=_listing_payload(session), stage=session.stage)


def start(session):
    session.stage = "interview"
    session.q_index = 0
    session.audit = None
    session.published = False
    session.price = None
    session.pending_facts = []
    return _reply(
        [
            INTRO,
            "I'll always ask a few required questions " + ", ".join(REQUIRED_KEYS) + " - then a couple of optional ones.",
            _current_question(session)["question"],
        ],
        _current_question(session)["quick_replies"],
        stage=session.stage,
    )


def _handle_english(session, text):
    text = (text or "").strip()
    if not text:
        return _reply("Please send a message or a voice note.", stage=session.stage)

    if session.stage == "idle":
        return start(session)

    if session.stage != "idle" and is_new_listing(text) and not is_publish(text):
        session.ledger = Ledger()
        session.photo = None
        session.audit = None
        return start(session)

    if session.stage == "review" and not is_publish(text) and is_new_listing(text):
        session.ledger = Ledger()
        session.photo = None
        return start(session)

    if session.stage == "photo":
        if is_new_listing(text):
            session.ledger = Ledger()
            session.photo = None
            return start(session)
        return _reply(
            "Please send a *photo* of this piece - I need it before I can publish to your shop page.",
            [],
            stage="photo",
        )

    if session.stage == "confirm":
        if is_confirm(text):
            if session.pending_facts:
                session.ledger.add_many(session.pending_facts, source_turn=session.q_index)
                for fact in session.pending_facts:
                    if fact.get("type") == "price":
                        session.price = _parse_price(fact.get("text", ""))
            session.pending_facts = []
            return _advance(session)
        if is_retry(text):
            return _reply("No problem - please send the correct details for this one.", [], stage="confirm")
        if is_non_answer(text):
            session.pending_facts = []
            return _advance(session)
        session.pending_facts = _extract(session, text) or [{"type": _current_question(session)["key"], "text": text}]
        return _ask_confirm(session)

    if session.stage == "interview":
        if is_bare_confirm(text):
            question = _current_question(session)
            return _reply(question["question"], question["quick_replies"], stage="interview")
        if is_non_answer(text):
            session.pending_facts = []
            key = _current_question(session)["key"]
            reply = _advance(session)
            reply["messages"].insert(0, f"Understood - I won't make any claim about {key}.")
            return reply
        session.pending_facts = _extract(session, text) or [{"type": _current_question(session)["key"], "text": text}]
        return _ask_confirm(session)

    if session.stage == "review":
        if is_publish(text):
            if not session.photo:
                session.stage = "photo"
                return _reply(
                    "I still need a *photo* of this piece before publishing. Please send it now.",
                    [],
                    stage="photo",
                )
            publish_product(session)
            return _reply(
                ["Published. Share this buyer page - it answers care, the exact piece, why it takes longer, and natural variations, all from your confirmed facts."],
                ["Start a new listing"],
                listing=_listing_payload(session),
                stage=session.stage,
                buyer_path=f"/buyer/{quote(session.id)}",
            )
        facts = extract_facts("correction", "The maker is correcting the listing.", text) or [{"type": "general", "text": text}]
        session.ledger.add_many(facts)
        return _listing_reply(session, prefix="Updated with your correction.")

    return start(session)


def handle_message(session, text):
    original = (text or "").strip()
    if original:
        english, language = translate_to_english(original)
        if language and language.lower() != "english":
            session.language_name = language
        if english:
            text = english
    reply = _handle_english(session, text)
    language = getattr(session, "language_name", "English")
    if language and language.lower() != "english":
        reply["messages"] = [translate_from_english(message, language) for message in reply["messages"]]
    return reply


# --------------------------------------------------------------------------
# Transport (Twilio)
# --------------------------------------------------------------------------
def _twiml(messages):
    body = "".join(f"<Message>{escape(m)}</Message>" for m in messages if m)
    return f"<?xml version='1.0' encoding='UTF-8'?><Response>{body}</Response>"


def _twiml_single(messages):
    text = "\n\n".join(m for m in messages if m)
    body = f"<Message>{escape(text)}</Message>" if text else ""
    return f"<?xml version='1.0' encoding='UTF-8'?><Response>{body}</Response>"


def send_whatsapp(to, body):
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM):
        return False
    try:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
        response = httpx.post(
            url,
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            data={"From": TWILIO_WHATSAPP_FROM, "To": to, "Body": body},
            timeout=20,
        )
        return response.status_code < 300
    except Exception:
        return False


# --------------------------------------------------------------------------
# Web app
# --------------------------------------------------------------------------
app = FastAPI(title="Source-Truth Listings")

SHARED_CSS = """
:root{--g-dark:#075e54;--g:#128c7e;--bin:#fff;--bout:#dcf8c6;--bg:#e5ddd5;--ink:#111b21;--muted:#667781;--danger:#b3261e;--ok:#1f7a3d;--warn:#a15c00}
*{box-sizing:border-box}
body{margin:0;font-family:"Segoe UI",system-ui,-apple-system,sans-serif;background:#0b141a;color:var(--ink);display:flex;justify-content:center;min-height:100vh}
.phone{width:100%;max-width:480px;height:100vh;display:flex;flex-direction:column;background:var(--bg);box-shadow:0 0 40px rgba(0,0,0,.5)}
.wa-header{background:var(--g-dark);color:#fff;display:flex;align-items:center;gap:12px;padding:10px 14px}
.avatar{width:40px;height:40px;border-radius:50%;background:var(--g);display:grid;place-items:center;font-weight:700}
.header-text{flex:1}.header-text .title{font-size:16px;font-weight:600}.header-text .subtitle{font-size:12px;opacity:.8}
.ghost{background:transparent;border:none;color:#fff;font-size:20px;cursor:pointer;opacity:.9}.ghost:hover{opacity:1}
.chat{flex:1;overflow-y:auto;padding:16px 12px 8px;display:flex;flex-direction:column;gap:8px}
.msg{display:flex}.msg.out{justify-content:flex-end}
.bubble{max-width:88%;padding:8px 11px 6px;border-radius:10px;background:var(--bin);box-shadow:0 1px 1px rgba(0,0,0,.12);animation:pop .16s ease-out}
.msg.out .bubble{background:var(--bout)}
@keyframes pop{from{transform:translateY(4px);opacity:0}to{transform:none;opacity:1}}
.bubble .text{white-space:pre-wrap;font-size:14.5px;line-height:1.4}.bubble .meta{font-size:10px;color:var(--muted);text-align:right;margin-top:2px}
.typing .bubble{font-style:italic;color:var(--muted)}
.quick{display:flex;gap:8px;padding:6px 12px;overflow-x:auto;background:var(--bg)}.quick:empty{display:none}
.chip{flex:0 0 auto;border:1px solid var(--g);color:var(--g-dark);background:#f3fff9;border-radius:16px;padding:6px 12px;font-size:13px;cursor:pointer;white-space:nowrap}.chip:hover{background:#e2f7ec}
.composer{display:flex;gap:8px;align-items:center;padding:8px 10px;background:#f0f0f0}
.composer input[type=text]{flex:1;border:none;border-radius:22px;padding:11px 16px;font-size:14.5px;outline:none}
.send,.mic{border:none;width:42px;height:42px;border-radius:50%;cursor:pointer;font-size:17px}
.send{background:var(--g);color:#fff}.mic{background:#e3e3e3;color:#4a4a4a}.mic.recording{background:var(--danger);color:#fff;animation:pulse 1s infinite}
@keyframes pulse{50%{opacity:.6}}
.card{background:#fff;border-radius:12px;overflow:hidden;margin-top:8px;border:1px solid #e2e2e2}
.card h3{margin:0;padding:10px 12px;background:var(--g-dark);color:#fff;font-size:15px}
.card .body{padding:10px 12px}.card .row{margin-bottom:8px}.card .row .label{font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:var(--muted)}.card .row .value{font-size:14px;line-height:1.4}
.card .flagged{background:#fff6f5;border-top:1px solid #f3d4d1;padding:10px 12px}.card .flagged h4{margin:0 0 6px;font-size:12px;color:var(--danger);text-transform:uppercase}
.claim{font-size:13px;padding:4px 0;display:flex;gap:8px;align-items:flex-start}.claim .tag{flex:0 0 auto;font-size:10px;font-weight:700;padding:2px 6px;border-radius:6px;text-transform:uppercase}
.tag.unsupported{background:#fde1df;color:var(--danger)}.tag.cultural_unverified{background:#ffeccc;color:var(--warn)}.tag.supported{background:#dcf5e4;color:var(--ok)}.tag.neutral{background:#eceaea;color:var(--muted)}
.buyer-link{display:inline-block;margin:8px 12px;background:var(--g);color:#fff;text-decoration:none;padding:8px 14px;border-radius:20px;font-size:13px}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(20px);background:rgba(0,0,0,.82);color:#fff;padding:9px 16px;border-radius:18px;font-size:13px;opacity:0;pointer-events:none;transition:all .25s}.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.buyer-page{background:#f7f4ef}.buyer-wrap{max-width:640px;margin:0 auto;padding:24px 16px 60px}.buyer-wrap h1{font-size:26px;margin:0 0 4px}.buyer-wrap .tagline{color:var(--muted);margin-bottom:18px}
.buyer-section{background:#fff;border-radius:12px;padding:14px 16px;margin-bottom:12px;box-shadow:0 1px 2px rgba(0,0,0,.06)}.buyer-section h2{font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:var(--g-dark);margin:0 0 6px}.buyer-section p{margin:0;line-height:1.5}
.provenance{font-size:12px;color:var(--muted);margin-top:6px;border-top:1px dashed #e5e5e5;padding-top:6px}
.trust-banner{background:#e8f5ec;border:1px solid #bfe3ca;color:#14532d;border-radius:12px;padding:12px 14px;font-size:13px;margin-bottom:16px}
.removed-note{background:#fff6f5;border:1px solid #f3d4d1;color:#7f1d1d;border-radius:12px;padding:12px 14px;font-size:13px;margin-top:12px}
"""

FRONTEND_JS = r"""
const chat=document.getElementById("chat"),quick=document.getElementById("quick"),input=document.getElementById("input"),sendBtn=document.getElementById("send"),micBtn=document.getElementById("mic"),photoBtn=document.getElementById("photoBtn"),photoFile=document.getElementById("photoFile"),resetBtn=document.getElementById("reset"),sampleBtn=document.getElementById("sample"),statusEl=document.getElementById("status"),toast=document.getElementById("toast");
let sessionId=localStorage.getItem("stl_session")||null,busy=false,pendingQ=[],seenQ={},seenOrders={};
const LABELS=[["story","Story"],["materials","Materials"],["care","Care"],["production","Production time"],["variations","Natural variations"],["cultural_note","Cultural note"],["photo_note","The exact piece"],["buyer_faq","Buyer FAQ"]];
function esc(v){return String(v||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")}
function now(){return new Date().toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"})}
function addBubble(text,out){const w=document.createElement("div");w.className="msg"+(out?" out":"");const b=document.createElement("div");b.className="bubble";const t=document.createElement("div");t.className="text";t.innerHTML=esc(text).replace(/\*(.+?)\*/g,"<strong>$1</strong>");const m=document.createElement("div");m.className="meta";m.textContent=now();b.appendChild(t);b.appendChild(m);w.appendChild(b);chat.appendChild(w);chat.scrollTop=chat.scrollHeight;return w}
function addTyping(){const w=document.createElement("div");w.className="msg typing";w.innerHTML='<div class="bubble"><div class="text">typing...</div></div>';chat.appendChild(w);chat.scrollTop=chat.scrollHeight;return w}
function addListingCard(listing){const card=document.createElement("div");card.className="card";const safe=listing.safe||{};let html=`<h3>${esc(safe.title||"Your listing")}</h3><div class="body">`;for(const[f,l]of LABELS){if(safe[f])html+=`<div class="row"><div class="label">${l}</div><div class="value">${esc(safe[f])}</div></div>`}html+="</div>";if(listing.removed&&listing.removed.length){html+='<div class="flagged"><h4>Blocked by the source-truth guard</h4>';for(const c of listing.removed)html+=`<div class="claim"><span class="tag ${esc(c.status)}">${esc(c.status)}</span><span>${esc(c.claim)}</span></div>`;html+="</div>"}card.innerHTML=html;if(listing.published&&listing.buyer_path){const a=document.createElement("a");a.className="buyer-link";a.href=listing.buyer_path;a.target="_blank";a.rel="noopener";a.textContent="Open buyer page";card.appendChild(a)}chat.appendChild(card);chat.scrollTop=chat.scrollHeight}
function setQuick(r){quick.innerHTML="";(r||[]).forEach(t=>{const c=document.createElement("button");c.className="chip";c.textContent=t;c.onclick=()=>send(t);quick.appendChild(c)})}
function showToast(m){toast.textContent=m;toast.classList.add("show");setTimeout(()=>toast.classList.remove("show"),2200)}
function handleResponse(resp){if(resp.session_id){sessionId=resp.session_id;localStorage.setItem("stl_session",sessionId)}if(resp.buyer_url&&resp.listing&&resp.listing.published)resp.listing.buyer_path=resp.buyer_url;(resp.messages||[]).forEach(t=>addBubble(t,false));if(resp.listing)addListingCard(resp.listing);setQuick(resp.quick_replies);if(resp.mock_llm)statusEl.textContent="maker assistant · offline mock"}
async function sendSellerAnswer(text){busy=true;addBubble(text,true);const typing=addTyping();try{const res=await fetch("/api/seller/answer",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({session_id:sessionId,answer:text})});const d=await res.json();typing.remove();if(d.ok){pendingQ.shift();addBubble("Sent to the buyer and saved to the knowledge base.",false)}else addBubble("No pending question to answer.",false)}catch(e){typing.remove();addBubble("Could not send that answer.",false)}finally{busy=false}}
async function pollSeller(){try{const res=await fetch("/api/seller/questions?session_id="+(sessionId||""));const d=await res.json();const fresh=(d.questions||[]).filter(q=>!seenQ[q.question_id]);fresh.forEach(q=>{seenQ[q.question_id]=1;pendingQ.push(q);addBubble("Buyer question: "+q.question+"\n\n(Reply here - your answer goes to the buyer and into the knowledge base.)",false)});if(fresh.length)setQuick(["Publish","Fix something"])}catch(e){}}
async function pollOrders(){try{const res=await fetch("/api/orders?session_id="+(sessionId||""));const d=await res.json();(d.orders||[]).forEach(o=>{if(seenOrders[o.id])return;seenOrders[o.id]=1;addBubble("NEW ORDER for '"+o.title+"'\nQty: "+o.quantity+(o.price?("\nPrice: "+o.price):"")+"\nBuyer: "+o.name+(o.contact?("\nContact: "+o.contact):"")+(o.note?("\nNote: "+o.note):"")+"\n\nReach the buyer on WhatsApp to confirm.",false)})}catch(e){}}
async function send(text){if(busy||!text)return;const low=text.toLowerCase();if(pendingQ.length&&low!=="publish"&&!low.includes("new listing")){return sendSellerAnswer(text)}busy=true;setQuick([]);addBubble(text,true);const typing=addTyping();try{const res=await fetch("/api/chat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({session_id:sessionId,message:text})});const data=await res.json();typing.remove();handleResponse(data)}catch(e){typing.remove();addBubble("Connection error. Is the server running?",false)}finally{busy=false}}
sendBtn.onclick=()=>{const v=input.value.trim();input.value="";send(v)};
input.addEventListener("keydown",e=>{if(e.key==="Enter"){const v=input.value.trim();input.value="";send(v)}});
resetBtn.onclick=async()=>{await fetch("/api/reset",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({session_id:sessionId})});localStorage.removeItem("stl_session");sessionId=null;chat.innerHTML="";setQuick([]);send("hi")};
sampleBtn.onclick=async()=>{if(busy)return;busy=true;setQuick([]);chat.innerHTML="";addBubble("Show a sample maker",true);const typing=addTyping();try{const res=await fetch("/api/sample",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({session_id:sessionId})});const data=await res.json();typing.remove();handleResponse(data)}catch(e){typing.remove();addBubble("Could not load sample.",false)}finally{busy=false}};
let recorder=null,chunks=[];
micBtn.onclick=async()=>{if(recorder&&recorder.state==="recording"){recorder.stop();return}try{const stream=await navigator.mediaDevices.getUserMedia({audio:true});recorder=new MediaRecorder(stream);chunks=[];recorder.ondataavailable=e=>chunks.push(e.data);recorder.onstop=async()=>{micBtn.classList.remove("recording");stream.getTracks().forEach(t=>t.stop());await sendVoice(new Blob(chunks,{type:"audio/webm"}))};recorder.start();micBtn.classList.add("recording");showToast("Recording... tap again to send")}catch(e){showToast("Microphone not available")}};
async function sendVoice(blob){busy=true;addBubble("[voice note]",true);const typing=addTyping();const form=new FormData();form.append("session_id",sessionId||"");form.append("audio",blob,"voice.webm");try{const res=await fetch("/api/voice",{method:"POST",body:form});const data=await res.json();typing.remove();if(data.transcript)addBubble("Transcript: "+data.transcript,false);handleResponse(data)}catch(e){typing.remove();addBubble("Could not send voice note.",false)}finally{busy=false}}
photoBtn.onclick=()=>photoFile.click();
photoFile.onchange=()=>{const f=photoFile.files[0];if(f)sendPhoto(f);photoFile.value=""};
async function sendPhoto(file){busy=true;const typing=addTyping();const form=new FormData();form.append("session_id",sessionId||"");form.append("image",file);try{const res=await fetch("/api/photo",{method:"POST",body:form});const data=await res.json();typing.remove();handleResponse(data)}catch(e){typing.remove();addBubble("Could not send photo.",false)}finally{busy=false}}
window.addEventListener("DOMContentLoaded",()=>{send("hi");setInterval(pollSeller,5000);setInterval(pollOrders,5000)});
"""

INDEX_HTML = (
    "<!doctype html><html lang='en'><head><meta charset='utf-8'/>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'/>"
    "<title>Source-Truth Listings</title><style>" + SHARED_CSS + "</style></head><body>"
    "<div class='phone'><header class='wa-header'><div class='avatar'>ST</div>"
    "<div class='header-text'><div class='title'>Source-Truth Listings</div>"
    "<div class='subtitle' id='status'>maker assistant · online</div></div>"
    "<button class='ghost' id='sample' title='Load sample demo'>&#9654;</button>"
    "<button class='ghost' id='reset' title='New listing'>&#8635;</button></header>"
    "<main id='chat' class='chat'></main><div id='quick' class='quick'></div>"
    "<footer class='composer'><button class='mic' id='photoBtn' title='Send photo'>&#128247;</button>"
    "<button class='mic' id='mic' title='Record voice note'>&#127908;</button>"
    "<input id='input' type='text' placeholder='Type a message' autocomplete='off'/>"
    "<button class='send' id='send' title='Send'>&#10148;</button>"
    "<input id='photoFile' type='file' accept='image/*' hidden/></footer></div>"
    "<div id='toast' class='toast'></div>"
    "<script>" + FRONTEND_JS + "</script></body></html>"
)

BUYER_HTML = (
    "<!doctype html><html lang='en'><head><meta charset='utf-8'/>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'/>"
    "<title>Listing · Source-Truth</title><style>" + SHARED_CSS + "</style></head>"
    "<body class='buyer-page'><div class='buyer-wrap'>"
    "<div class='trust-banner'>Every statement below is traced to facts the maker confirmed. Nothing is generated or invented.</div>"
    "<h1 id='title'>Loading...</h1><div class='tagline' id='tagline'>Handmade by the maker</div>"
    "<div id='sections'></div><div id='removed'></div>"
    "<div class='buyer-section' style='margin-top:16px'><h2>Confirmed facts used</h2><div id='facts'></div></div>"
    "</div><script>"
    "const SID=window.location.pathname.split('/').filter(Boolean).pop();"
    "const LABELS={story:'Story',materials:'Materials',care:'Care',production:'Production time',variations:'Natural variations',cultural_note:'Cultural note',photo_note:'The exact piece',buyer_faq:'Buyer questions'};"
    "const FL={identity:'Identity',material:'Material',care:'Care',process:'Process',variation:'Variation',photo:'Photo',cultural:'Cultural',provenance:'Provenance',general:'General'};"
    "function esc(v){return String(v||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}"
    "fetch('/api/listing/'+SID).then(r=>r.json()).then(d=>{if(d.error){document.getElementById('title').textContent='Listing not ready yet';return}"
    "const s=d.safe||{};document.getElementById('title').textContent=s.title||'Handmade piece';document.getElementById('tagline').textContent='Handmade · one of a kind';"
    "if(d.photo_url){const img=document.createElement('img');img.src=d.photo_url;img.alt='Product photo';img.style.cssText='width:100%;border-radius:12px;margin-bottom:16px';document.querySelector('.buyer-wrap').insertBefore(img,document.getElementById('title'))}"
    "const sec=document.getElementById('sections');for(const f of Object.keys(LABELS)){if(!s[f])continue;const div=document.createElement('div');div.className='buyer-section';div.innerHTML='<h2>'+LABELS[f]+'</h2><p>'+esc(s[f])+'</p>';sec.appendChild(div)}"
    "if(d.removed&&d.removed.length){const n=document.createElement('div');n.className='removed-note';n.innerHTML='<strong>Guarded for your trust:</strong> '+d.removed.length+' claim(s) the maker did not confirm were blocked from this listing instead of being invented.';document.getElementById('removed').appendChild(n)}"
    "const facts=document.getElementById('facts');(d.facts||[]).forEach(x=>{const div=document.createElement('div');div.className='provenance';div.textContent=(FL[x.type]||x.type)+': '+x.text;facts.appendChild(div)})"
    "}).catch(()=>{document.getElementById('title').textContent='Could not load listing'});"
    "</script></body></html>"
)

SHOP_EXTRA_CSS = """
.shop-page{background:#f7f4ef;display:block}
.shop-header{background:#075e54;color:#fff;display:flex;justify-content:space-between;align-items:center;padding:14px 20px}
.shop-header .brand{color:#fff;text-decoration:none;font-weight:700;font-size:18px}
.shop-header .seller-link{color:#cdebe4;text-decoration:none;font-size:13px}
.shop-wrap{max-width:1000px;margin:0 auto;padding:24px 16px 60px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px}
.product-card{background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.08);text-decoration:none;color:inherit;display:flex;flex-direction:column}
.product-card img,.product-card .noimg{width:100%;height:200px;object-fit:cover;background:#e8e8e8;display:block}
.product-card .pc-body{padding:12px 14px}
.product-card h3{margin:0 0 4px;font-size:16px}
.product-card p{margin:0;color:#667781;font-size:13px}
.product-card .pc-link{margin-top:10px;color:#128c7e;font-weight:600;font-size:13px}
.empty{color:#667781;text-align:center;padding:60px 0;font-size:15px}
.qa-wrap{max-width:640px;margin:28px auto 0}
.qa-wrap h2{font-size:15px;color:#075e54;margin:0 0 10px}
.qa-item{background:#fff;border-radius:12px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 2px rgba(0,0,0,.06)}
.qa-item .q{font-weight:600}
.qa-item .a{margin-top:6px}
.qa-item .pending{color:#a15c00;font-size:13px;margin-top:6px}
.chat-input{display:flex;gap:8px;position:sticky;bottom:0;background:#f7f4ef;padding:12px 0}
.chat-input input{flex:1;border:1px solid #ddd;border-radius:22px;padding:12px 16px;font-size:14px;outline:none}
.chat-input button{background:#128c7e;color:#fff;border:none;border-radius:22px;padding:0 22px;font-size:14px;cursor:pointer}
.product-card .price{color:#075e54;font-weight:700;margin-top:6px;font-size:15px}
.addbtn{width:100%;margin-top:10px;background:#e7f6f1;color:#075e54;border:1px solid #bfe3ca;border-radius:10px;padding:9px;font-size:13px;cursor:pointer;font-weight:600}
.addbtn:hover{background:#d6efe7}
.buybtn{background:#128c7e;color:#fff;border:none;border-radius:24px;padding:13px 28px;font-size:15px;cursor:pointer;font-weight:600}
.cartbar{display:none;position:sticky;bottom:0;background:#075e54;color:#fff;padding:12px 16px;align-items:center;justify-content:space-between;border-radius:12px 12px 0 0;margin-top:20px}
.cartbar button{background:#25d366;color:#08331f;border:none;border-radius:22px;padding:10px 20px;font-size:14px;font-weight:700;cursor:pointer}
"""

SHOP_JS = r"""
function esc(v){return String(v||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")}
const grid=document.getElementById("grid");
fetch("/api/catalog").then(r=>r.json()).then(d=>{
  if(!d.products.length){grid.innerHTML='<div class="empty">No products yet. Ask the maker to publish a listing, then reload.</div>';return}
  grid.innerHTML=d.products.map(p=>'<a class="product-card" href="'+p.path+'">'+(p.photo_url?'<img src="'+p.photo_url+'" alt="">':'<span class="noimg"></span>')+'<div class="pc-body">'+(p.price?'<div class="price">\u20b9'+esc(p.price)+'</div>':'')+(p.colour?'<div class="colour">Colour: '+esc(p.colour)+'</div>':'')+'<div class="pc-link">View &amp; ask a question &rarr;</div></div></a>').join("");
}).catch(()=>{grid.innerHTML='<div class="empty">Could not load products.</div>'});
"""

SHOP_HTML = (
    "<!doctype html><html lang='en'><head><meta charset='utf-8'/>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'/>"
    "<title>Artisan Market</title><style>" + SHARED_CSS + SHOP_EXTRA_CSS + "</style></head>"
    "<body class='shop-page'><header class='shop-header'><a href='/shop' class='brand'>Artisan Market</a>"
    "<a href='/' class='seller-link'>I'm a maker</a></header>"
    "<div class='shop-wrap'><div id='grid' class='grid'></div></div>"
    "<script>" + SHOP_JS + "</script></body></html>"
)

PRODUCT_JS = r"""
const PID=window.location.pathname.split("/").filter(Boolean).pop();
const QLABELS=[["story","Story"],["materials","Materials"],["care","Care"],["production","Production time"],["variations","Natural variations"],["cultural_note","Cultural note"],["photo_note","The exact piece"],["buyer_faq","Buyer questions"]];
let buyerId=localStorage.getItem("buyer_id");if(!buyerId){buyerId="b"+Math.random().toString(36).slice(2,10);localStorage.setItem("buyer_id",buyerId)}
function esc(v){return String(v||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")}
function renderListing(d){
  if(d.error){document.getElementById("detail").innerHTML="<h1>Listing not ready</h1>";return}
  const s=d.safe||{};let html="<div class='trust-banner'>Every statement below is traced to facts the maker confirmed. Nothing is invented.</div>";
  if(d.photo_url)html+="<img src='"+d.photo_url+"' alt='' style='width:100%;border-radius:12px;margin-bottom:14px'/>";
  html+="<h1>"+esc(s.title||"Handmade piece")+"</h1><div class='tagline'>Handmade &middot; one of a kind</div>";
  if(d.price)html+="<div class='price' style='font-size:20px;margin:6px 0 4px'>\u20b9"+esc(d.price)+"</div>";
  for(const f of QLABELS){if(s[f[0]])html+="<div class='buyer-section'><h2>"+f[1]+"</h2><p>"+esc(s[f[0]])+"</p></div>"}
  html+="<div style='margin:18px 0'><button id='buybtn' class='buybtn'>Buy now</button></div>";
  document.getElementById("detail").innerHTML=html;
  const b=document.getElementById("buybtn");if(b)b.onclick=buyNow;
}
function buyNow(){const name=prompt("Your name?")||"A buyer";const contact=prompt("Your phone or email?")||"";const note=prompt("Any message for the maker? (optional)")||"";fetch("/api/buyer/order",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({product_id:PID,buyer_id:buyerId,name:name,contact:contact,note:note})}).then(r=>r.json()).then(res=>{alert("Order placed! The maker has been notified on WhatsApp. Reference: "+res.order_id)})}
function loadQa(){fetch("/api/buyer/thread/"+PID).then(r=>r.json()).then(d=>{
  const box=document.getElementById("qa");
  box.innerHTML=(d.qa||[]).map(qaHtml).join("")||"<p style='color:#667781'>No questions yet. Ask the maker anything.</p>";
})}
function qaHtml(item){return "<div class='qa-item'><div class='q'>Q: "+esc(item.question)+"</div>"+(item.answer?"<div class='a'>A: "+esc(item.answer)+"</div>":"<div class='pending'>Seller is checking &hellip; the answer will appear here.</div>")+"</div>"}
function ask(){const inp=document.getElementById("q");const q=inp.value.trim();if(!q)return;inp.value="";
  fetch("/api/buyer/ask",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({product_id:PID,buyer_id:buyerId,question:q})})
   .then(r=>r.json()).then(res=>{loadQa();if(res.status==="pending")startPoll()})}
let pollTimer=null;
function startPoll(){if(pollTimer)return;pollTimer=setInterval(()=>{fetch("/api/buyer/thread/"+PID).then(r=>r.json()).then(d=>{loadQa();if(!(d.qa||[]).some(x=>x.status==="pending")){clearInterval(pollTimer);pollTimer=null}})},3000)}
window.addEventListener("DOMContentLoaded",()=>{
  fetch("/api/listing/"+PID).then(r=>r.json()).then(renderListing).catch(()=>{});
  loadQa();
  document.getElementById("ask").onclick=ask;
  document.getElementById("q").addEventListener("keydown",e=>{if(e.key==="Enter")ask()});
});
"""

PRODUCT_HTML = (
    "<!doctype html><html lang='en'><head><meta charset='utf-8'/>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'/>"
    "<title>Product &middot; Artisan Market</title><style>" + SHARED_CSS + SHOP_EXTRA_CSS + "</style></head>"
    "<body class='shop-page'><header class='shop-header'><a href='/shop' class='brand'>Artisan Market</a>"
    "<a href='/' class='seller-link'>I'm a maker</a></header>"
    "<div class='shop-wrap'><div id='detail' class='buyer-wrap' style='padding:0'></div>"
    "<div class='qa-wrap'><h2>Ask the maker</h2><div id='qa'></div>"
    "<div class='chat-input'><input id='q' placeholder='e.g. Is it machine washable?'/>"
    "<button id='ask'>Ask</button></div></div></div>"
    "<script>" + PRODUCT_JS + "</script></body></html>"
)


def _absolute(request, path):
    if not path:
        return ""
    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}{path}"
    return str(request.base_url).rstrip("/") + path


def _respond(request, session, reply):
    return {
        "session_id": session.id,
        "messages": reply["messages"],
        "quick_replies": reply["quick_replies"],
        "listing": reply["listing"],
        "stage": reply["stage"],
        "buyer_url": _absolute(request, reply.get("buyer_path") or (f"/buyer/{quote(session.id)}" if reply["listing"] else "")),
        "mock_llm": not LLM_ENABLED,
    }


@app.get("/")
def index():
    return HTMLResponse(INDEX_HTML)


@app.get("/buyer/{sid}")
def buyer(sid: str):
    return HTMLResponse(BUYER_HTML)


@app.get("/shop")
def shop():
    return HTMLResponse(SHOP_HTML)


@app.get("/shop/{sid}")
def shop_product(sid: str):
    return HTMLResponse(PRODUCT_HTML)


@app.get("/api/health")
def health():
    return {"ok": True, "llm_enabled": LLM_ENABLED, "twilio_enabled": bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN)}


@app.post("/api/chat")
async def chat(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "invalid payload"}, status_code=400)
    session = store.get(payload.get("session_id"))
    return _respond(request, session, handle_message(session, payload.get("message", "")))


@app.post("/api/sample")
async def sample(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    session = store.get(payload.get("session_id"))
    session.ledger = Ledger()
    session.ledger.add_many(SAMPLE_FACTS)
    session.q_index = len(QUESTIONS)
    session.price = _parse_price(next((f["text"] for f in SAMPLE_FACTS if f["type"] == "price"), ""))
    session.photo = {"bytes": _SAMPLE_PHOTO, "content_type": "image/svg+xml"}
    reply = _listing_reply(session, prefix=f"Sample maker: {len(SAMPLE_FACTS)} confirmed facts loaded.")
    return _respond(request, session, reply)


@app.post("/api/reset")
async def reset(request: Request):
    payload = await request.json()
    sid = payload.get("session_id")
    if sid:
        store.remove(sid)
    return {"reset": True}


@app.post("/api/voice")
async def voice(request: Request, session_id: str = Form(""), audio: UploadFile = File(...)):
    session = store.get(session_id or None)
    text = transcribe(await audio.read(), audio.filename or "audio.webm")
    if not text:
        return {
            "session_id": session.id,
            "messages": ["I could not read that voice note. Please type your answer, or set OPENAI_API_KEY to enable transcription."],
            "quick_replies": [],
            "listing": None,
            "stage": session.stage,
            "buyer_url": "",
            "mock_llm": not LLM_ENABLED,
            "transcript": "",
        }
    result = _respond(request, session, handle_message(session, text))
    result["transcript"] = text
    return result


@app.get("/api/listing/{sid}")
def listing(sid: str):
    session = store.get(sid)
    if not session.audit:
        return JSONResponse({"error": "not ready"}, status_code=404)
    return {
        "session_id": session.id,
        "safe": session.audit["safe"],
        "draft": session.audit["draft"],
        "removed": session.audit["removed"],
        "claims": session.audit["claims"],
        "facts": session.ledger.all(),
        "published": session.published,
        "photo_url": _media_url(session),
        "price": session.price,
    }


_SAMPLE_PHOTO = (
    b"<svg xmlns='http://www.w3.org/2000/svg' width='600' height='450'>"
    b"<rect width='600' height='450' fill='#284b8c'/>"
    b"<text x='300' y='235' font-family='sans-serif' font-size='34' fill='#fff' "
    b"text-anchor='middle'>Indigo Shawl</text></svg>"
)


def _media_url(session):
    return f"/media/{quote(session.id)}" if session.photo else None


@app.get("/api/catalog")
def catalog():
    products = []
    for session in store.all():
        if not session.published or not session.audit:
            continue
        safe = session.audit["safe"]
        products.append(
            {
                "id": session.id,
                "title": safe.get("title") or "Handmade piece",
                "colour": _session_colour(session),
                "price": session.price,
                "photo_url": _media_url(session),
                "path": f"/shop/{quote(session.id)}",
            }
        )
    return {"products": products}


def _session_colour(session):
    for fact in session.ledger.to_facts():
        if fact.get("type") == "colour":
            return fact.get("text", "")
    return ""


@app.post("/api/buyer/ask")
async def buyer_ask(request: Request):
    payload = await request.json()
    session = store.get(payload.get("product_id"))
    if not session.audit:
        return JSONResponse({"error": "unknown product"}, status_code=404)
    question = (payload.get("question") or "").strip()
    if not question:
        return JSONResponse({"error": "empty question"}, status_code=400)
    entry = ask_buyer_question(session, question, payload.get("buyer_id"))
    return {
        "status": entry["status"],
        "answer": entry["answer"],
        "question_id": entry["id"],
        "seller_notified": entry["status"] == "pending",
    }


@app.get("/api/buyer/thread/{product_id}")
def buyer_thread(product_id: str):
    session = store.get(product_id)
    return {"qa": session.qa}


def _normalize_phone(value):
    import re as _re

    raw = (value or "").strip().lower()
    digits = _re.sub(r"[^0-9]", "", raw)
    if digits:
        return digits
    return raw


def _seller_key(session):
    return _normalize_phone(session.seller_phone or session.id)


def _seller_numbers(session):
    """All identifiers that can refer to this seller (browser session + phone)."""
    ids = {_normalize_phone(session.id), _normalize_phone(session.seller_phone)}
    return {x for x in ids if x}


def _has_phone(keys):
    return any(k and k.isdigit() and len(k) >= 8 for k in keys)


def _pending_ids_for(session):
    """Pending buyer-question ids for this seller, matched by any known identifier."""
    keys = _seller_numbers(session)
    # also include the seller phone of any product this session published
    for s in store.all():
        if s.id == session.id or (s.seller_phone and _normalize_phone(s.seller_phone) in keys):
            keys |= _seller_numbers(s)
    ids = []
    matched = False
    for key, qids in PENDING_BY_SELLER.items():
        if key in keys:
            ids.extend(qids)
            matched = True
    if not _has_phone(keys):
        # generic browser seller with no linked phone: show everything (single-seller demo)
        ids = [qid for qids in PENDING_BY_SELLER.values() for qid in qids]
    return ids


@app.get("/api/seller/questions")
def seller_questions(session_id: str = ""):
    session = store.get(session_id or None)
    items = []
    for qid in _pending_ids_for(session):
        record = BUYER_QUESTIONS.get(qid)
        if record:
            items.append(
                {
                    "question_id": qid,
                    "question": record["entry"]["question"],
                    "product_id": record["product_id"],
                }
            )
    return {"session_id": session.id, "questions": items}


@app.post("/api/seller/answer")
async def seller_answer(request: Request):
    payload = await request.json()
    session = store.get(payload.get("session_id"))
    entry = answer_seller_question(_seller_key(session), payload.get("answer", ""))
    if not entry:
        return JSONResponse({"error": "no pending question"}, status_code=404)
    return {"ok": True, "question": entry["question"], "answer": entry["answer"], "product_id": session.id}


@app.post("/api/buyer/order")
async def buyer_order(request: Request):
    payload = await request.json()
    session = store.get(payload.get("product_id"))
    if not session.audit:
        return JSONResponse({"error": "unknown product"}, status_code=404)
    title = (session.audit.get("safe", {}) or {}).get("title") or "your product"
    try:
        quantity = max(1, int(payload.get("quantity") or 1))
    except Exception:
        quantity = 1
    buyer_name = (payload.get("name") or "").strip() or "A buyer"
    buyer_contact = (payload.get("contact") or "").strip()
    note = (payload.get("note") or "").strip()
    order = {
        "id": uuid.uuid4().hex[:10],
        "product_id": session.id,
        "title": title,
        "buyer_id": payload.get("buyer_id") or "guest",
        "name": buyer_name,
        "contact": buyer_contact,
        "note": note,
        "quantity": quantity,
        "price": session.price,
        "status": "placed",
        "created": time.time(),
    }
    seller = session.seller_phone or session.id
    order["seller"] = _normalize_phone(seller)
    ORDERS[order["id"]] = order
    notified = False
    if str(seller).startswith("whatsapp:"):
        lines = [f"New order for '{title}'", f"Quantity: {quantity}"]
        if session.price:
            lines.append(f"Price: {session.price}")
        lines.append(f"Buyer: {buyer_name}")
        if buyer_contact:
            lines.append(f"Contact: {buyer_contact}")
        if note:
            lines.append(f"Note: {note}")
        notified = send_whatsapp(seller, "\n".join(lines))
    return {"order_id": order["id"], "status": "placed", "seller_notified": notified}


@app.get("/api/orders")
def orders(session_id: str = ""):
    session = store.get(session_id or None)
    keys = _seller_numbers(session)
    # include identifiers of any product/session that shares this seller's phone
    for s in store.all():
        numbers = _seller_numbers(s)
        if numbers & keys:
            keys |= numbers
    mine = [
        o
        for o in ORDERS.values()
        if _normalize_phone(o.get("seller")) in keys
        or _normalize_phone(o["product_id"]) in keys
    ]
    if not _has_phone(keys):
        # generic browser seller: show all orders (single-seller demo)
        mine = list(ORDERS.values())
    return {"orders": mine}


@app.get("/media/{sid}")
def media(sid: str):
    session = store.get(sid)
    if not session.photo:
        return Response(status_code=404)
    return Response(content=session.photo["bytes"], media_type=session.photo["content_type"])


async def _download_media(url):
    auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN) else None
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(url, auth=auth, follow_redirects=True)
        return response.content


@app.post("/api/photo")
async def photo(request: Request, session_id: str = Form(""), image: UploadFile = File(...)):
    session = store.get(session_id or None)
    data = await image.read()
    content_type = image.content_type or "image/jpeg"
    session.photo = {"bytes": data, "content_type": content_type}
    message = "Got your photo - I'll show this exact photo on the buyer page."
    quick = []
    if session.stage == "interview" and _current_question(session)["key"] == "photo":
        session.pending_facts = [
            {"type": "photo", "text": "The attached photo shows the exact piece the buyer will receive."}
        ]
        message += " Is that correct?"
        session.stage = "confirm"
        quick = ["Yes", "No, let me fix it"]
    elif session.stage == "photo":
        reply = _listing_reply(session, prefix="Photo received. Here is your listing.")
        return _respond(request, session, reply)
    return {
        "session_id": session.id,
        "messages": [message],
        "quick_replies": quick,
        "listing": _listing_payload(session) if session.audit else None,
        "stage": session.stage,
        "buyer_url": "",
        "mock_llm": not LLM_ENABLED,
    }


@app.post("/webhook/twilio")
async def twilio_webhook(request: Request):
    form = dict((await request.form()).items())
    media_count = int(form.get("NumMedia", "0") or "0")
    user_id = (form.get("From", "") or "").strip().lower()
    session = store.get(user_id)
    text = (form.get("Body", "") or "").strip()

    for index in range(media_count):
        media_url = form.get(f"MediaUrl{index}")
        content_type = form.get(f"MediaContentType{index}", "") or ""
        if not media_url:
            continue
        try:
            data = await _download_media(media_url)
        except Exception:
            continue
        if content_type.startswith("image"):
            session.photo = {"bytes": data, "content_type": content_type}
        elif content_type.startswith("audio") or content_type in ("video/ogg", "application/ogg"):
            text = transcribe(data, "audio.ogg") or text

    if session.stage == "photo" and session.photo:
        reply = _listing_reply(session, prefix="Photo received. Here is your listing.")
        return Response(content=_twiml(reply["messages"]), media_type="application/xml")

    if (
        PENDING_BY_SELLER.get(_normalize_phone(user_id))
        and not is_publish(text)
        and not is_new_listing(text)
        and session.stage not in ("interview", "confirm", "photo")
    ):
        entry = answer_seller_question(user_id, text)
        if entry:
            return Response(
                content=_twiml_single(["Thanks - that has been sent to the buyer and added to the knowledge base."]),
                media_type="application/xml",
            )

    reply = handle_message(session, text)

    messages = list(reply["messages"])
    if reply.get("buyer_path") and reply["listing"] and reply["listing"].get("published"):
        base = PUBLIC_BASE_URL
        if not base:
            host = request.headers.get("host", "")
            if host:
                base = f"https://{host}"
        url = (base or "") + reply["buyer_path"]
        if url:
            messages.append(f"Buyer page: {url}")
    return Response(content=_twiml_single(messages), media_type="application/xml")


_tunnel_proc = None


def _find_cloudflared():
    from shutil import which

    found = which("cloudflared")
    if found:
        return found
    candidates = [
        r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
        r"C:\Program Files\cloudflared\cloudflared.exe",
        os.path.expanduser(r"~\cloudflared\cloudflared.exe"),
        "/usr/local/bin/cloudflared",
        "/usr/bin/cloudflared",
        "/opt/homebrew/bin/cloudflared",
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None


def _start_cloudflared(port):
    exe = _find_cloudflared()
    if not exe:
        return None
    proc = subprocess.Popen(
        [exe, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    deadline = time.time() + 40
    for line in proc.stdout:
        match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if match:
            return match.group(0), proc
        if time.time() > deadline:
            break
    proc.terminate()
    return None


def _start_pyngrok(port):
    try:
        importlib.import_module("pyngrok")
    except ImportError:
        print("Installing pyngrok for the public tunnel...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyngrok"])
    from pyngrok import conf, ngrok

    token = os.getenv("NGROK_AUTHTOKEN", "").strip()
    if token:
        conf.get_default().auth_token = token
    tunnel = ngrok.connect(port, "http")
    public_url = tunnel.public_url
    if public_url.startswith("http://"):
        public_url = "https://" + public_url[len("http://"):]
    return public_url.rstrip("/")


def start_tunnel(port):
    global PUBLIC_BASE_URL, _tunnel_proc
    result = _start_cloudflared(port)
    if result:
        url, proc = result
        _tunnel_proc = proc
    else:
        url = _start_pyngrok(port)
    PUBLIC_BASE_URL = url
    return PUBLIC_BASE_URL


def main():
    import uvicorn

    live = "--live" in sys.argv or os.getenv("LIVE", "").strip().lower() in {"1", "true", "yes"}
    host = "0.0.0.0" if live else os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    local_url = f"http://127.0.0.1:{port}"

    print("=" * 66)
    print("  Source-Truth Listings")
    print(f"  Open:  {local_url}")
    print(f"  Mode:  {'OpenAI (' + OPENAI_MODEL + ')' if LLM_ENABLED else 'offline mock (set OPENAI_API_KEY for real AI)'}")

    public_url = PUBLIC_BASE_URL
    if live:
        try:
            public_url = start_tunnel(port)
            print(f"  Public: {public_url}")
            print("")
            print("  WhatsApp (Twilio sandbox):")
            print("   1. Twilio Console > Messaging > Try it out > Send a WhatsApp message")
            print("   2. Join the sandbox from your phone with the code Twilio shows")
            print(f"   3. Set the inbound webhook (POST) to: {public_url}/webhook/twilio")
            print("   4. Message the sandbox - photos and voice notes work")
        except Exception as exc:
            print(f"  Tunnel failed: {exc}")
            print("  Fix it one of these ways, then re-run:")
            print("    a) Set NGROK_AUTHTOKEN in .env  (free: https://dashboard.ngrok.com/get-started/your-authtoken)")
            print("    b) Install cloudflared (winget install --id Cloudflare.cloudflared) - no account needed")
            print(f"    c) Run your own tunnel, then set PUBLIC_BASE_URL=https://... and restart")
            print("  The app is still running locally; WhatsApp just needs the public URL.")
    print("=" * 66)

    if os.getenv("NO_BROWSER", "").strip().lower() not in {"1", "true", "yes"}:
        threading.Timer(1.8, lambda: webbrowser.open(local_url)).start()
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
