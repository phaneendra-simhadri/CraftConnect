from . import openai_client


def generate_listing(facts: list[dict]) -> dict:
    draft = openai_client.draft_listing(facts)
    for field in openai_client.LISTING_FIELDS:
        draft.setdefault(field, "")
    return draft
