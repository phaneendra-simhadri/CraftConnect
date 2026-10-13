from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import agent, buyer, interview, openai_client, transport
from .config import settings
from .sessions import store

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"

app = FastAPI(title="Source-Truth Listings")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")


def _absolute(request: Request, path: str) -> str:
    if not path:
        return ""
    if settings.public_base_url:
        return f"{settings.public_base_url}{path}"
    return str(request.base_url).rstrip("/") + path


def _respond(request: Request, session, reply: dict) -> dict:
    return {
        "session_id": session.id,
        "messages": reply["messages"],
        "quick_replies": reply["quick_replies"],
        "listing": reply["listing"],
        "stage": reply["stage"],
        "buyer_url": _absolute(
            request,
            reply.get("buyer_path") or (f"/buyer/{quote(session.id)}" if reply["listing"] else ""),
        ),
        "mock_llm": not settings.llm_enabled,
    }


def _media_url(session) -> str | None:
    return f"/media/{quote(session.id)}" if session.photo else None


def _session_colour(session) -> str:
    for fact in session.ledger.to_facts():
        if fact.get("type") == "colour":
            return fact.get("text", "")
    return ""


@app.get("/")
def index():
    return FileResponse(str(FRONTEND / "index.html"))


@app.get("/buyer/{sid}")
def buyer_page(sid: str):
    return FileResponse(str(FRONTEND / "buyer.html"))


@app.get("/shop")
def shop():
    return FileResponse(str(FRONTEND / "shop.html"))


@app.get("/shop/{sid}")
def shop_product(sid: str):
    return FileResponse(str(FRONTEND / "product.html"))


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "llm_enabled": settings.llm_enabled,
        "twilio_enabled": settings.twilio_enabled,
    }


@app.post("/api/chat")
async def chat(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "invalid payload"}, status_code=400)
    session = store.get(payload.get("session_id"))
    reply = agent.handle_message(session, payload.get("message", ""))
    return _respond(request, session, reply)


@app.post("/api/sample")
async def sample(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    session = store.get(payload.get("session_id"))
    reply = agent.load_sample(session)
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
    text = openai_client.transcribe(await audio.read(), audio.filename or "audio.webm")
    if not text:
        return {
            "session_id": session.id,
            "messages": [
                "I could not read that voice note. Please type your answer, or set "
                "OPENAI_API_KEY to enable transcription."
            ],
            "quick_replies": [],
            "listing": None,
            "stage": session.stage,
            "buyer_url": "",
            "mock_llm": not settings.llm_enabled,
            "transcript": "",
        }
    reply = agent.handle_message(session, text)
    result = _respond(request, session, reply)
    result["transcript"] = text
    return result


@app.post("/api/photo")
async def photo(request: Request, session_id: str = Form(""), image: UploadFile = File(...)):
    session = store.get(session_id or None)
    data = await image.read()
    session.photo = {"bytes": data, "content_type": image.content_type or "image/jpeg"}
    message = agent.handle_photo(session)
    return {
        "session_id": session.id,
        "messages": [message],
        "quick_replies": ["Yes", "No, let me fix it"] if session.stage == "confirm" else [],
        "listing": agent._listing_payload(session) if session.audit else None,
        "stage": session.stage,
        "buyer_url": "",
        "mock_llm": not settings.llm_enabled,
    }


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


@app.post("/api/buyer/ask")
async def buyer_ask(request: Request):
    payload = await request.json()
    session = store.get(payload.get("product_id"))
    if not session.audit:
        return JSONResponse({"error": "unknown product"}, status_code=404)
    question = (payload.get("question") or "").strip()
    if not question:
        return JSONResponse({"error": "empty question"}, status_code=400)
    entry = buyer.ask_buyer_question(session, question, payload.get("buyer_id"))
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


@app.get("/api/seller/questions")
def seller_questions(session_id: str = ""):
    session = store.get(session_id or None)
    return {"session_id": session.id, "questions": buyer.seller_questions(session)}


@app.post("/api/seller/answer")
async def seller_answer(request: Request):
    payload = await request.json()
    session = store.get(payload.get("session_id"))
    entry = buyer.answer_seller_question(buyer.seller_key(session), payload.get("answer", ""))
    if not entry:
        return JSONResponse({"error": "no pending question"}, status_code=404)
    return {
        "ok": True,
        "question": entry["question"],
        "answer": entry["answer"],
        "product_id": session.id,
    }


@app.post("/api/buyer/order")
async def buyer_order(request: Request):
    payload = await request.json()
    session = store.get(payload.get("product_id"))
    if not session.audit:
        return JSONResponse({"error": "unknown product"}, status_code=404)
    return buyer.create_order(session, payload)


@app.get("/api/orders")
def orders(session_id: str = ""):
    session = store.get(session_id or None)
    return {"orders": buyer.orders_for(session)}


@app.get("/media/{sid}")
def media(sid: str):
    session = store.get(sid)
    if not session.photo:
        return Response(status_code=404)
    return Response(content=session.photo["bytes"], media_type=session.photo["content_type"])


async def _download_media(url: str) -> bytes:
    auth = None
    if settings.twilio_enabled:
        auth = (settings.twilio_account_sid, settings.twilio_auth_token)
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(url, auth=auth, follow_redirects=True)
        return response.content


@app.post("/webhook/twilio")
async def twilio_webhook(request: Request):
    form = dict((await request.form()).items())
    media_count = int(form.get("NumMedia", "0") or "0")
    user_id = transport.normalize_number(form.get("From", ""))
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
            text = openai_client.transcribe(data, "audio.ogg") or text

    if (
        buyer.PENDING_BY_SELLER.get(user_id)
        and not interview.is_publish(text)
        and not interview.is_new_listing(text)
        and (session.stage == "idle" or media_count > 0)
    ):
        entry = buyer.answer_seller_question(user_id, text)
        if entry:
            return Response(
                content=transport.twiml(
                    ["Thanks - that has been sent to the buyer and added to the knowledge base."]
                ),
                media_type="application/xml",
            )

    reply = agent.handle_message(session, text)
    messages = list(reply["messages"])
    if reply.get("buyer_path") and reply["listing"] and reply["listing"].get("published"):
        url = (settings.public_base_url or "") + reply["buyer_path"]
        if url:
            messages.append(f"Buyer page: {url}")
    return Response(content=transport.twiml(messages), media_type="application/xml")
