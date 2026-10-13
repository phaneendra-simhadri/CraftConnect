"""Buyer knowledge-base Q&A, seller escalation, and orders.

Mirrors the loop in app.py: a buyer question is answered from confirmed facts
(plus previously answered questions) or escalated to the seller on WhatsApp.
Seller answers become confirmed facts, so the knowledge base learns.
"""

import time
import uuid

from . import openai_client, transport
from .sessions import store

BUYER_QUESTIONS: dict[str, dict] = {}
PENDING_BY_SELLER: dict[str, list[str]] = {}
ORDERS: dict[str, dict] = {}


def seller_key(session) -> str:
    return session.seller_phone or session.id


def publish_product(session) -> str:
    session.published = True
    if not session.seller_phone:
        session.seller_phone = session.id
    return session.seller_phone


def ask_buyer_question(session, question: str, buyer_id: str | None) -> dict:
    answer = openai_client.kb_answer(question, session.ledger.to_facts(), session.qa)
    entry = {
        "id": uuid.uuid4().hex[:10],
        "question": (question or "").strip(),
        "answer": answer or "",
        "status": "answered" if answer else "pending",
        "buyer_id": buyer_id or "guest",
        "created": time.time(),
    }
    session.qa.append(entry)
    if not answer:
        BUYER_QUESTIONS[entry["id"]] = {"product_id": session.id, "entry": entry}
        PENDING_BY_SELLER.setdefault(seller_key(session), []).append(entry["id"])
        seller = seller_key(session)
        if str(seller).startswith("whatsapp:"):
            title = (session.audit or {}).get("safe", {}).get("title", "your product")
            transport.send_whatsapp(
                seller,
                f"A buyer asked about '{title}':\n\"{entry['question']}\"\n\n"
                "Reply with the answer (text or voice note).",
            )
    return entry


def answer_seller_question(seller: str, text: str) -> dict | None:
    pending = PENDING_BY_SELLER.get(seller) or []
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
    answer, _language = openai_client.translate_to_english(answer)
    facts = openai_client.extract_facts("buyer_question", entry["question"], answer)
    if not facts:
        facts = [{"type": "general", "text": answer}]
    session.ledger.add_many(facts)
    entry["answer"] = answer
    entry["status"] = "answered"
    entry["answered"] = time.time()
    pending.pop(0)
    BUYER_QUESTIONS.pop(qid, None)
    return entry


def create_order(session, payload: dict) -> dict:
    title = (session.audit or {}).get("safe", {}).get("title") or "your product"
    try:
        quantity = max(1, int(payload.get("quantity") or 1))
    except Exception:
        quantity = 1
    order = {
        "id": uuid.uuid4().hex[:10],
        "product_id": session.id,
        "title": title,
        "buyer_id": payload.get("buyer_id") or "guest",
        "name": (payload.get("name") or "").strip() or "A buyer",
        "contact": (payload.get("contact") or "").strip(),
        "note": (payload.get("note") or "").strip(),
        "quantity": quantity,
        "price": session.price,
        "status": "placed",
        "created": time.time(),
    }
    ORDERS[order["id"]] = order
    seller = seller_key(session)
    notified = False
    if str(seller).startswith("whatsapp:"):
        lines = [f"New order for '{title}'", f"Quantity: {quantity}"]
        if session.price:
            lines.append(f"Price: {session.price}")
        lines.append(f"Buyer: {order['name']}")
        if order["contact"]:
            lines.append(f"Contact: {order['contact']}")
        if order["note"]:
            lines.append(f"Note: {order['note']}")
        notified = transport.send_whatsapp(seller, "\n".join(lines))
    return {"order_id": order["id"], "status": "placed", "seller_notified": notified}


def seller_questions(session) -> list[dict]:
    items = []
    for qid in PENDING_BY_SELLER.get(seller_key(session), []):
        record = BUYER_QUESTIONS.get(qid)
        if record:
            items.append(
                {
                    "question_id": qid,
                    "question": record["entry"]["question"],
                    "product_id": record["product_id"],
                }
            )
    return items


def orders_for(session) -> list[dict]:
    return [o for o in ORDERS.values() if o["product_id"] == session.id]
