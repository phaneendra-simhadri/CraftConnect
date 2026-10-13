from urllib.parse import quote

from . import buyer
from . import draft as draft_module
from . import guard, interview, openai_client
from .ledger import Ledger
from .sessions import Session

INTRO = (
    "Hi! I turn your craft knowledge into a buyer-ready listing where nothing is invented. "
    "Every claim stays traceable to you."
)

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
    return {
        "messages": messages,
        "quick_replies": quick_replies or [],
        "listing": listing,
        "stage": stage,
        "buyer_path": buyer_path,
    }


def _current_question(session: Session) -> dict:
    return interview.QUESTIONS[session.q_index]


def _ask_question(session: Session):
    question = _current_question(session)
    session.stage = "interview"
    return _reply(question["question"], question["quick_replies"], stage=session.stage)


def _ask_confirm(session: Session):
    lines = "\n".join(f"- {f['text']}" for f in session.pending_facts)
    question = _current_question(session)
    message = (
        f"Got it. I'll record this as *{question['key']}*:\n{lines}\n\nIs that correct?"
    )
    session.stage = "confirm"
    return _reply(
        message,
        ["Yes", "No, let me fix it"],
        stage=session.stage,
    )


def _extract(session: Session, text: str):
    question = _current_question(session)
    return openai_client.extract_facts(question["key"], question["question"], text)


def _commit_pending(session: Session):
    if session.pending_facts:
        session.ledger.add_many(session.pending_facts, source_turn=session.q_index)
        for fact in session.pending_facts:
            if fact.get("type") == "price":
                session.price = openai_client.parse_price(fact.get("text", ""))
    session.pending_facts = []


def _advance(session: Session):
    session.q_index += 1
    if session.q_index >= len(interview.QUESTIONS):
        return _listing_reply(session, prefix="All facts confirmed.")
    return _ask_question(session)


def _run_audit(session: Session):
    facts = session.ledger.to_facts()
    draft = draft_module.generate_listing(facts)
    session.audit = guard.audit(draft, facts)
    return session.audit


def _listing_payload(session: Session):
    audit = session.audit
    return {
        "safe": audit["safe"],
        "removed": audit["removed"],
        "claims": audit["claims"],
        "published": session.published,
        "buyer_path": f"/buyer/{quote(session.id)}",
    }


def _listing_reply(session: Session, prefix: str = ""):
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
        value = safe.get(field)
        if value:
            lines.append(f"*{label}:* {value}")
    if safe.get("buyer_faq"):
        lines.append(f"*Buyer FAQ:* {safe['buyer_faq']}")
    if removed:
        lines.append(
            f"\nI flagged or removed {len(removed)} claim(s) I could not verify against your facts:"
        )
        for claim in removed:
            lines.append(f"- ({claim['type']}) {claim['claim']}")
    lines.append("\nReply *Publish* to create the buyer link, or send a correction.")
    session.stage = "review"
    return _reply(
        lines,
        ["Publish", "Fix something"],
        listing=_listing_payload(session),
        stage=session.stage,
    )


def start(session: Session):
    session.stage = "interview"
    session.q_index = 0
    session.audit = None
    session.published = False
    return _reply(
        [
            INTRO,
            "I'll always ask a few required questions "
            + ", ".join(interview.REQUIRED_KEYS)
            + " - then a couple of optional ones.",
            _current_question(session)["question"],
        ],
        _current_question(session)["quick_replies"],
        stage=session.stage,
    )


def _handle_english(session: Session, text: str):
    text = (text or "").strip()
    if not text:
        return _reply("Please send a message or a voice note.", stage=session.stage)
    session.log("user", text)

    if session.stage == "idle":
        return start(session)

    if session.stage == "review" and not interview.is_publish(text) and interview.is_new_listing(text):
        session.ledger = Ledger()
        return start(session)

    if session.stage == "confirm":
        if interview.is_confirm(text):
            _commit_pending(session)
            return _advance(session)
        if interview.is_retry(text):
            return _reply(
                "No problem - please send the correct details for this one.",
                [],
                stage="confirm",
            )
        if openai_client.is_non_answer(text):
            session.pending_facts = []
            return _advance(session)
        session.pending_facts = _extract(session, text)
        if not session.pending_facts:
            session.pending_facts = [{"type": _current_question(session)["key"], "text": text}]
        return _ask_confirm(session)

    if session.stage == "interview":
        if openai_client.is_bare_confirm(text):
            return _reply(_current_question(session)["question"], _current_question(session)["quick_replies"], stage="interview")
        if openai_client.is_non_answer(text):
            session.pending_facts = []
            key = _current_question(session)["key"]
            reply = _advance(session)
            reply["messages"].insert(0, f"Understood - I won't make any claim about {key}.")
            return reply
        session.pending_facts = _extract(session, text)
        if not session.pending_facts:
            session.pending_facts = [{"type": _current_question(session)["key"], "text": text}]
        return _ask_confirm(session)

    if session.stage == "review":
        if interview.is_publish(text):
            buyer.publish_product(session)
            return _reply(
                [
                    "Published. Share this buyer page - it answers care, the exact piece, "
                    "why it takes longer, and natural variations, all from your confirmed facts.",
                ],
                ["Start a new listing"],
                listing=_listing_payload(session),
                stage=session.stage,
                buyer_path=f"/buyer/{quote(session.id)}",
            )
        facts = openai_client.extract_facts("correction", "The maker is correcting the listing.", text)
        if not facts:
            facts = [{"type": "general", "text": text}]
        session.ledger.add_many(facts)
        return _listing_reply(session, prefix="Updated with your correction.")

    return start(session)


def load_sample(session: Session):
    session.ledger = Ledger()
    session.ledger.add_many(SAMPLE_FACTS)
    session.q_index = len(interview.QUESTIONS)
    session.price = openai_client.parse_price(
        next((f["text"] for f in SAMPLE_FACTS if f["type"] == "price"), "")
    )
    return _listing_reply(
        session, prefix=f"Sample maker: {len(SAMPLE_FACTS)} confirmed facts loaded."
    )


def handle_photo(session: Session) -> str:
    message = "Got your photo - I'll show this exact photo on the buyer page."
    if session.stage == "interview" and _current_question(session)["key"] == "photo":
        session.pending_facts = [
            {"type": "photo", "text": "The attached photo shows the exact piece the buyer will receive."}
        ]
        message += " Is that correct?"
        session.stage = "confirm"
    return message


def handle_message(session: Session, text: str):
    original = (text or "").strip()
    if original:
        english, language = openai_client.translate_to_english(original)
        if language and language.lower() != "english":
            session.language_name = language
        if english:
            text = english
    reply = _handle_english(session, text)
    language = getattr(session, "language_name", "English")
    if language and language.lower() != "english":
        reply["messages"] = [
            openai_client.translate_from_english(message, language) for message in reply["messages"]
        ]
    return reply
