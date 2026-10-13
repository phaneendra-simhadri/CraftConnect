from . import openai_client

CARE_FALLBACK = "Care for this piece is not yet confirmed - ask the maker."
CULTURAL_FALLBACK = (
    "This pattern holds significance in the maker's community; its detailed meaning "
    "is not documented here."
)

FIELD_ORDER = openai_client.LISTING_FIELDS
ALLOWED_STATUS = {"supported", "unsupported", "cultural_unverified"}


def _normalize_status(status: str) -> str:
    status = (status or "").strip().lower()
    return status if status in ALLOWED_STATUS else "unsupported"


def enforce(claims: list[dict], ledger_facts: list[dict]) -> list[dict]:
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


def reassemble(draft: dict, claims: list[dict]) -> dict:
    grouped: dict[str, list[str]] = {}
    for claim in claims:
        field = claim.get("field") or "story"
        if field not in FIELD_ORDER:
            field = "story"
        text = claim.get("claim", "").strip()
        if claim["status"] in ("supported", "neutral"):
            kept = text
        else:
            kept = claim.get("replacement", "").strip()
        if not kept:
            continue
        grouped.setdefault(field, [])
        if kept not in grouped[field]:
            grouped[field].append(kept)

    safe = {}
    for field in FIELD_ORDER:
        safe[field] = " ".join(grouped.get(field, [])).strip()
    safe["title"] = (draft.get("title") or safe.get("title") or "Handmade piece").strip()
    return safe


def audit(draft: dict, ledger_facts: list[dict]) -> dict:
    raw_claims = openai_client.audit_listing(draft, ledger_facts)
    claims = enforce(raw_claims, ledger_facts)
    safe = reassemble(draft, claims)

    ledger_types = {f["type"] for f in ledger_facts}
    if "care" not in ledger_types and not safe.get("care"):
        safe["care"] = CARE_FALLBACK
    if "cultural" not in ledger_types and not safe.get("cultural_note"):
        safe["cultural_note"] = CULTURAL_FALLBACK

    removed = [
        c
        for c in claims
        if c["status"] != "supported" and c["type"] != "neutral" and c.get("field") != "title"
    ]
    return {
        "draft": draft,
        "safe": safe,
        "claims": claims,
        "removed": removed,
    }
