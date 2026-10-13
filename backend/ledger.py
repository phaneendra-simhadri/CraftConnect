class Ledger:
    def __init__(self):
        self._facts = []
        self._counter = 0

    def add(self, fact_type: str, text: str, source_turn: int = 0) -> dict:
        text = (text or "").strip()
        if not text:
            return {}
        existing = {(f["type"], f["text"].lower()) for f in self._facts}
        if (fact_type, text.lower()) in existing:
            return {}
        self._counter += 1
        fact = {
            "id": self._counter,
            "type": fact_type,
            "text": text,
            "source_turn": source_turn,
            "confirmed": True,
        }
        self._facts.append(fact)
        return fact

    def add_many(self, facts: list[dict], source_turn: int = 0) -> list[dict]:
        added = []
        for fact in facts:
            item = self.add(fact.get("type", "general"), fact.get("text", ""), source_turn)
            if item:
                added.append(item)
        return added

    def all(self) -> list[dict]:
        return list(self._facts)

    def by_type(self, fact_type: str) -> list[dict]:
        return [f for f in self._facts if f["type"] == fact_type]

    def has_type(self, fact_type: str) -> bool:
        return any(f["type"] == fact_type for f in self._facts)

    def is_empty(self) -> bool:
        return not self._facts

    def to_facts(self) -> list[dict]:
        return [{"type": f["type"], "text": f["text"]} for f in self._facts]
