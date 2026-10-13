"""End-to-end smoke test for the single-file app (app.py).

Run: python -m scripts.smoke_single

Exercises the maker interview (required + optional questions), the guard, publish,
the buyer knowledge-base Q&A, WhatsApp escalation, seller answers, and orders.
"""

import sys

from fastapi.testclient import TestClient

import app as single

client = TestClient(single.app)
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


def main():
    health = client.get("/api/health").json()
    check("health endpoint", health.get("ok") is True)
    check("shop page", client.get("/shop").status_code == 200)

    check("required questions defined", len(single.REQUIRED_KEYS) >= 4, str(single.REQUIRED_KEYS))
    check("delivery is compulsory", "delivery" in single.REQUIRED_KEYS)
    check("making_time is compulsory", "making_time" in single.REQUIRED_KEYS)

    first = chat("hi")
    sid = first["session_id"]
    check("interview starts", first["stage"] == "interview")

    answers = [q["quick_replies"][0] for q in single.QUESTIONS]
    reply = None
    for answer in answers:
        chat(answer, sid)
        reply = chat("Yes", sid)
    check("asks for a product photo", reply["stage"] == "photo", reply.get("stage"))

    photo = client.post(
        "/api/photo",
        data={"session_id": sid},
        files={"image": ("piece.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
    ).json()
    check("photo upload reaches review", photo["stage"] == "review", str(photo.get("stage")))
    reply = photo

    removed = reply["listing"]["removed"]
    check("guard reports blocked claims", all(c.get("status") != "supported" for c in removed), str(removed)[:100])

    safe = reply["listing"]["safe"]
    check("confirmed care kept", "wash" in (safe.get("care") or "").lower(), safe.get("care"))
    check("care field not invented", "machine washable" not in (safe.get("care") or "").lower(), safe.get("care"))
    check("cultural not invented", "prosperity" not in (safe.get("cultural_note") or "").lower(), safe.get("cultural_note"))

    listing = client.get(f"/api/listing/{sid}").json()
    check("price captured", listing["price"] == 1200, str(listing.get("price")))
    check("photo stored", bool(listing.get("photo_url")))
    facts = {f["type"] for f in listing["facts"]}
    check("delivery fact stored", "delivery" in facts, str(facts))
    check("making_time fact stored", "making_time" in facts, str(facts))
    check("colour fact stored", "colour" in facts, str(facts))

    published = chat("Publish", sid)
    check("publish works", (published.get("listing") or {}).get("published") is True)

    catalog = client.get("/api/catalog").json()
    product = next((p for p in catalog["products"] if p["id"] == sid), None)
    check("catalog has product", product is not None)
    check("catalog shows price", product and product["price"] == 1200)
    check("catalog shows colour", product and product.get("colour"), str(product.get("colour")) if product else "")
    check("catalog shows photo", product and product.get("photo_url"))

    # publish is blocked until a photo exists
    s2 = chat("hi")["session_id"]
    for answer in answers:
        chat(answer, s2)
        chat("Yes", s2)
    blocked = chat("Publish", s2)
    check("publish blocked without photo", blocked["stage"] == "photo", blocked.get("stage"))

    sample = client.post("/api/sample", json={}).json()
    sample_listing = client.get(f"/api/listing/{sample['session_id']}").json()
    check("sample carries price", sample_listing.get("price") == 1200, str(sample_listing.get("price")))

    # knowledge-base answered instantly
    a1 = client.post("/api/buyer/ask", json={"product_id": sid, "buyer_id": "b1", "question": "Is it machine washable?"}).json()
    check("care question answered", a1["status"] == "answered", str(a1))
    a2 = client.post("/api/buyer/ask", json={"product_id": sid, "buyer_id": "b1", "question": "When will my order arrive?"}).json()
    check("delivery question answered", a2["status"] == "answered", str(a2))
    a3 = client.post("/api/buyer/ask", json={"product_id": sid, "buyer_id": "b1", "question": "How long does it take to make?"}).json()
    check("making-time question answered", a3["status"] == "answered", str(a3))

    # unknown -> escalate to seller
    a4 = client.post("/api/buyer/ask", json={"product_id": sid, "buyer_id": "b1", "question": "Can you make it in bright pink?"}).json()
    check("unknown question escalated", a4["status"] == "pending", str(a4))
    seller_q = client.get(f"/api/seller/questions?session_id={sid}").json()
    check("seller has pending question", len(seller_q["questions"]) >= 1)

    answered = client.post("/api/seller/answer", json={"session_id": sid, "answer": "Yes, pink is possible and takes one extra week."}).json()
    check("seller answer accepted", answered.get("ok") is True)
    thread = client.get(f"/api/buyer/thread/{sid}").json()
    check("buyer sees answer", any(x["status"] == "answered" and "pink" in x["answer"].lower() for x in thread["qa"]))

    # order -> seller notification + seller order list
    order = client.post("/api/buyer/order", json={"product_id": sid, "buyer_id": "b1", "name": "Asha", "contact": "+910000000000", "quantity": 2}).json()
    check("order placed", bool(order.get("order_id")), str(order))
    orders = client.get(f"/api/orders?session_id={sid}").json()
    check("seller sees order", any(o["id"] == order["order_id"] for o in orders["orders"]))

    # reset / bad input / twilio
    check("reset works", client.post("/api/reset", json={"session_id": sid}).json()["reset"] is True)
    check("invalid json rejected", client.post("/api/chat", content=b"x", headers={"Content-Type": "application/json"}).status_code == 400)
    tw = client.post("/webhook/twilio", data={"From": "whatsapp:+910000000000", "Body": "hi", "NumMedia": "0"})
    check("twilio webhook", tw.status_code == 200 and "Response" in tw.text)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
