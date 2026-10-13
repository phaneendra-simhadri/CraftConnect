import time
import uuid

from .ledger import Ledger


class Session:
    def __init__(self, sid: str):
        self.id = sid
        self.ledger = Ledger()
        self.stage = "idle"
        self.q_index = 0
        self.pending_facts: list[dict] = []
        self.pending_question = None
        self.audit = None
        self.published = False
        self.photo = None
        self.seller_phone = None
        self.price = None
        self.qa: list[dict] = []
        self.language_name = "English"
        self.history: list[dict] = []
        self.created = time.time()

    def log(self, role: str, text: str) -> None:
        self.history.append({"role": role, "text": text, "ts": time.time()})


class SessionStore:
    def __init__(self):
        self._sessions: dict[str, Session] = {}

    def get(self, sid: str | None = None) -> Session:
        if sid and sid in self._sessions:
            return self._sessions[sid]
        sid = sid or uuid.uuid4().hex[:10]
        session = Session(sid)
        self._sessions[sid] = session
        return session

    def remove(self, sid: str) -> None:
        self._sessions.pop(sid, None)

    def reset(self, sid: str) -> Session:
        self.remove(sid)
        return self.get(sid)

    def all(self) -> list[Session]:
        return list(self._sessions.values())


store = SessionStore()
