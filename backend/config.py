import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _bool(value, default=False):
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "").strip()
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "").strip()
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    openai_transcribe_model: str = os.getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1")
    groq_api_key: str = os.getenv("GROQ_API_KEY", "").strip()
    groq_base_url: str = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
    groq_transcribe_model: str = os.getenv("GROQ_TRANSCRIBE_MODEL", "whisper-large-v3")
    twilio_account_sid: str = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    twilio_auth_token: str = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    twilio_whatsapp_from: str = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    use_mock_llm: bool = _bool(os.getenv("USE_MOCK_LLM"), False)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.openai_api_key) and not self.use_mock_llm

    @property
    def twilio_enabled(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token)


settings = Settings()
