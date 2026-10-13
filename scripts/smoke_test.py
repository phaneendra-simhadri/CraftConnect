"""End-to-end smoke test using FastAPI's TestClient. Run: python -m scripts.smoke_test"""

import sys

from fastapi.testclient import TestClient

from backend import interview
from backend.main import app
from backend.sessions import store

client = TestClient(app)
FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def chat(message, sid=None):
    body = {"message": message}
    if sid:
        body["session_id"] = sid
    return client.post("/api/chat", json=body).json()


ANSWERS = [
    "Handwoven wool shawl",
    "Cream and natural brown",
    "Pure wool with natural dyes",
    "One week per piece",
    "Ships in about two weeks",
    "Hand wash only, dry in shade",
    "Woven on a handloom",
    "Colour and texture vary slightly",
    "Yes, this is the exact piece, one of a kind",
    "A family pattern whose meaning I cannot confirm",
    "1200",
]


def run_interview(sid):
    reply = {}
    for answer in ANSWERS:
        reply = chat(answer, sid)
        reply = chat("Yes", sid)
    return reply


def main():
    health = client.get("/api/health").json()
    check("health endpoint", health.get("ok") is True)

    first = chat("hi")
    sid = first["session_id"]
    check("session created", bool(sid))
    check("interview starts", first["stage"] == "interview")

    reply = run_interview(sid)
    check("reaches review stage", reply["stage"] == "review", reply.get("stage"))
    removed = (reply.get("listing") or {}).get("removed", [])
    check("guard reports blocked claims", all(c.get("status") != "supported" for c in removed), str(removed)[:100])

    safe = (reply.get("listing") or {}).get("safe", {})
    check("confirmed care kept", "wash" in safe.get("care", "").lower(), safe.get("care"))
    check("invented washable blocked", "machine washable" not in safe.get("care", "").lower())
    check(
        "invented meaning blocked",
        "prosperity" not in safe.get("cultural_note", "").lower(),
        safe.get("cultural_note"),
    )

    published = chat("publish", sid)
    check("publish works", (published.get("listing") or {}).get("published") is True)
    check("buyer url present", bool(published.get("buyer_url")))

    listing = client.get(f"/api/listing/{sid}").json()
    facts = listing.get("facts", [])
    check("listing has facts", len(facts) >= len(ANSWERS) and len(facts) > 0, str(len(facts)))
    fact_types = {f["type"] for f in facts}
    check("core fact types captured", {"identity", "colour", "material", "care", "price"} <= fact_types, str(fact_types))
    check("listing has provenance claims", len(listing.get("claims", [])) > 0)

    buyer = client.get(f"/buyer/{sid}")
    check("buyer page served", buyer.status_code == 200)
    check("shop page served", client.get("/shop").status_code == 200)
    check("product page served", client.get(f"/shop/{sid}").status_code == 200)

    catalog = client.get("/api/catalog").json()
    product = next((p for p in catalog["products"] if p["id"] == sid), None)
    check("catalog has product", product is not None)
    check("catalog shows price", product and product["price"] == 1200)

    a1 = client.post(
        "/api/buyer/ask",
        json={"product_id": sid, "buyer_id": "b1", "question": "Is it machine washable?"},
    ).json()
    check("care question answered", a1["status"] == "answered", str(a1))
    a2 = client.post(
        "/api/buyer/ask",
        json={"product_id": sid, "buyer_id": "b1", "question": "When will my order arrive?"},
    ).json()
    check("delivery question answered", a2["status"] == "answered", str(a2))
    a3 = client.post(
        "/api/buyer/ask",
        json={"product_id": sid, "buyer_id": "b1", "question": "How long does it take to make?"},
    ).json()
    check("making-time question answered", a3["status"] == "answered", str(a3))
    a4 = client.post(
        "/api/buyer/ask",
        json={"product_id": sid, "buyer_id": "b1", "question": "Can you make it in bright pink?"},
    ).json()
    check("unknown question escalated", a4["status"] == "pending", str(a4))

    seller_q = client.get(f"/api/seller/questions?session_id={sid}").json()
    check("seller has pending question", len(seller_q["questions"]) >= 1)
    answered = client.post(
        "/api/seller/answer",
        json={"session_id": sid, "answer": "Yes, pink is possible and takes one extra week."},
    ).json()
    check("seller answer accepted", answered.get("ok") is True)
    thread = client.get(f"/api/buyer/thread/{sid}").json()
    check("buyer sees answer", any(x["status"] == "answered" and "pink" in x["answer"].lower() for x in thread["qa"]))

    order = client.post(
        "/api/buyer/order",
        json={"product_id": sid, "buyer_id": "b1", "name": "Asha", "contact": "+910000000000", "quantity": 2},
    ).json()
    check("order placed", bool(order.get("order_id")), str(order))
    orders = client.get(f"/api/orders?session_id={sid}").json()
    check("seller sees order", any(o["id"] == order["order_id"] for o in orders["orders"]))

    restarted = chat("Start a new listing", sid)
    check("restart begins fresh interview", restarted["stage"] == "interview")
    check("restart clears listing", restarted.get("listing") is None)

    r2 = chat("a cotton scarf", sid)
    check("new answer asked to confirm", r2["stage"] == "confirm")
    retry = chat("No, let me fix it", sid)
    check("retry keeps confirm stage", retry["stage"] == "confirm")
    check("retry does not record junk", "let me fix it" not in retry["messages"][0])

    voice = client.post(
        "/api/voice",
        data={"session_id": sid},
        files={"audio": ("voice.webm", b"fake-audio", "audio/webm")},
    ).json()
    check("voice endpoint responds", "messages" in voice)

    non = chat("hi")
    sid2 = non["session_id"]
    # Answer each question by matching its text; say "I don't know" for care and cultural.
    topup = {
        "identity": "Handwoven wool shawl",
        "colour": "Cream and natural brown",
        "material": "Pure wool with natural dyes",
        "making_time": "One week per piece",
        "delivery": "Ships in about two weeks",
        "care": "I don't know",
        "process": "Woven on a handloom",
        "variation": "Colour and texture vary slightly",
        "photo": "Yes, this is the exact piece, one of a kind",
        "cultural": "no idea",
        "price": "1200",
    }
    for _ in range(40):
        session2 = store.get(sid2)
        if session2.stage == "review":
            break
        key = interview.QUESTIONS[session2.q_index]["key"] if session2.q_index < len(interview.QUESTIONS) else None
        text = topup.get(key, "Yes")
        reply = chat(text, sid2)
        if reply["stage"] == "confirm":
            chat("Yes", sid2)
    listing2 = client.get(f"/api/listing/{sid2}").json()
    check(
        "care fallback when unconfirmed",
        "not yet confirmed" in listing2["safe"]["care"].lower(),
        listing2["safe"]["care"],
    )
    check(
        "cultural fallback when unconfirmed",
        "not documented" in listing2["safe"]["cultural_note"].lower(),
        listing2["safe"]["cultural_note"],
    )
    check(
        "non-answers not stored as facts",
        all("i don't know" not in f["text"].lower() and "no idea" not in f["text"].lower() for f in listing2["facts"]),
    )

    reset = client.post("/api/reset", json={"session_id": sid}).json()
    check("reset works", reset.get("reset") is True)

    bad = client.post(
        "/api/chat", content=b"not json", headers={"Content-Type": "application/json"}
    )
    check("invalid json rejected", bad.status_code == 400)

    twilio = client.post(
        "/webhook/twilio",
        data={"From": "whatsapp:+910000000000", "Body": "hi", "NumMedia": "0"},
    )
    check("twilio webhook status", twilio.status_code == 200)
    check("twilio returns twiml", "Response" in twilio.text and "Message" in twilio.text)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
