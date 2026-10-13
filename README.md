# Craft Connect

Craft Connect turns a short chat conversation with an artisan into a shop-ready product listing where every claim is traceable to the maker. It was built for an overnight hackathon.

The problem it addresses: an AI listing writer will happily invent care instructions, production times, or the meaning behind a traditional pattern. For handmade goods that is often simply wrong, and for heritage crafts it can misrepresent a community's work. Craft Connect flips the order. The maker is interviewed first, every answer is stored as a confirmed fact, and the generated listing is only allowed to use those facts.

## How it works

**Maker side**

1. The bot interviews the artisan through a WhatsApp-style chat. Compulsory questions cover identity, material, making time, delivery time, care, whether the photo shows the exact item, cultural meaning, and price. Process and natural variation are optional.
2. Each answer is extracted into a fact and read back for a one-tap confirmation. Only confirmed facts enter the Claim Ledger.
3. An LLM drafts a complete listing. This step is allowed to be persuasive, which is exactly where invented claims appear.
4. The guard splits the draft into atomic claims and checks each one against the ledger: supported claims are kept with their evidence, unsupported care or process claims are stripped, and invented cultural meaning is blocked outright.
5. The maker gets a buyer page that answers the questions buyers actually ask, using confirmed facts only.

**Buyer side**

6. Buyers open the storefront, browse published products, and open one.
7. A question typed by the buyer is looked up in the product's knowledge base of confirmed facts and previous answers.
8. A hit returns an instant answer. A miss is escalated to the maker over WhatsApp.
9. The maker replies with text or a voice note, which is turned into a fact, added to the knowledge base, and shown to the buyer.
10. The buyer can order from the product page. The order stores the price and contact details, and the maker is notified.

```
Maker (WhatsApp) -> FastAPI /webhook (or simulator) -> agent.py
                                                       | interview -> ledger
                                                       | draft (LLM)
                                                       | guard (audit + repair)
                                                       v
                                           product page /shop/{id}

Buyer -> ask -> knowledge base -- hit  --> instant answer
                               \-- miss --> escalate on WhatsApp -> maker reply
                                             -> new fact -> answer buyer
```

## Run it

The quickest path is the single-file app:

```powershell
python app.py
```

`app.py` installs any missing packages, starts the server, and opens the browser. It runs fully offline with a deterministic mock, and the mock deliberately inserts plausible-but-invented claims so you can watch the guard remove them without an API key.

Optional environment variables: `PORT` (default 8000), `HOST` (default `127.0.0.1`), and `NO_BROWSER=1` to skip launching the browser.

To use a real model, copy `.env.example` to `.env`, set `OPENAI_API_KEY`, and restart.

### Modular version

The same app is also split into `backend/` and `frontend/` if you prefer to work on it that way:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn backend.main:app --reload --port 8000
```

### Command-line demo and tests

```powershell
python -m scripts.demo         # full interview, guarded listing, blocked claims
python -m scripts.smoke_test   # modular app end-to-end
python -m scripts.smoke_single # single-file app.py end-to-end
```

Both smoke tests exit non-zero on failure.

## Going live on WhatsApp (Twilio)

The artisan side runs over WhatsApp and the buyer side is a plain web link, so the buyer never needs WhatsApp.

1. Create a Twilio account and open **Messaging > Try out WhatsApp**. Send the join code from your phone to the number Twilio provides.
2. Add credentials to `.env`:

   ```
   TWILIO_ACCOUNT_SID=AC...
   TWILIO_AUTH_TOKEN=...
   OPENAI_API_KEY=sk-...      # only needed for voice-note transcription
   ```

3. Start with a public tunnel:

   ```powershell
   python app.py --live
   ```

   The public HTTPS URL is printed. It prefers `cloudflared` if installed, otherwise ngrok.

4. In the **Try out WhatsApp** settings, set the inbound message webhook to `https://<your-url>/webhook/twilio` (POST, `application/x-www-form-urlencoded`), and save.
5. Message the number as the maker, then reply `Publish` to get the buyer link to share.

Sessions are keyed by phone number, so several testers can share a single number. Trial accounts can only message numbers that have joined, but buyers use the web link and do not need to join.

## Project layout

```
app.py                 single-file app (recommended): maker flow, storefront, Q&A, escalation
backend/
  main.py              FastAPI routes: maker, storefront, buyer Q&A, orders, Twilio webhook
  agent.py             interview -> draft -> guard -> reply, plus translation
  interview.py         compulsory and optional questions, reply keywords
  ledger.py            the confirmed-facts Claim Ledger
  draft.py             listing generation
  guard.py             claim audit and deterministic repair
  buyer.py             knowledge-base Q&A, seller escalation, orders
  openai_client.py     OpenAI wrapper, offline mock, KB answering, translation
  prompts.py           extraction, drafting, guard, KB and translation prompts
  transport.py         Twilio normalization, TwiML, WhatsApp send
  sessions.py          in-memory session store
frontend/
  index.html           WhatsApp-style simulator
  app.js               chat logic, quick replies, voice, seller Q&A, order polling
  buyer.html           buyer page with provenance
  shop.html            storefront listing all published products
  product.html         product detail with Q&A and a Buy now button
scripts/               demo and smoke tests
docs/                  architecture document, regenerated by scripts/make_docs.py
```

## How the guard works

- Default-deny. Unsupported claims are removed, not softened.
- Even if the auditor marks a care or cultural claim as supported, the guard overrides it unless the ledger holds a confirmed fact of that type.
- Unknown care becomes "Care for this piece is not yet confirmed - ask the maker." Unverified pattern meaning becomes "This pattern holds significance in the maker's community; its detailed meaning is not documented here."
- Supported claims carry the matching ledger fact so the buyer page can show provenance.

## Limitations

- Sessions live in memory, so a restart clears them. That is fine for a demo.
- The offline mock uses keyword heuristics. With an API key the LLM handles extraction, drafting, and auditing.
- Token-overlap matching in the mock is approximate; the LLM auditor is the intended production path.
