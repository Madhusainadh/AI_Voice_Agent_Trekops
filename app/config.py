"""Environment-backed settings for the calling agent.

Mirrors the conventions of the Node backend's src/config/index.js: everything is
read once at import time, defaults are safe, and nothing here throws on a missing
optional key — the component that needs it fails loudly instead.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── Service ──────────────────────────────────────────────────────────────
    port: int = 8090
    log_level: str = "INFO"
    enabled_company_codes: str = ""

    # ── WhatsApp / Meta ──────────────────────────────────────────────────────
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_webhook_verification_token: str = ""

    # ── Sarvam ───────────────────────────────────────────────────────────────
    sarvam_api_key: str = ""
    sarvam_stt_model: str = "saaras:v3"
    sarvam_stt_mode: str = "codemix"
    sarvam_tts_model: str = "bulbul:v2"
    sarvam_tts_voice: str = "anushka"
    sarvam_tts_language: str = "hi-IN"

    # ── LLM ──────────────────────────────────────────────────────────────────
    google_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    # ── TrekOps ──────────────────────────────────────────────────────────────
    mongo_uri: str = ""
    mongo_control_db: str = "admin"
    trekops_api_url: str = "http://127.0.0.1:8080"
    jwt_secret: str = ""
    service_user_id: str = "000000000000000000000000"

    agent_forward_secret: str = ""

    # ── Behaviour ────────────────────────────────────────────────────────────
    human_grace_seconds: int = 12
    max_call_seconds: int = 420

    @property
    def allowed_companies(self) -> set[str]:
        """Empty set means "no restriction"."""
        return {c.strip().lower() for c in self.enabled_company_codes.split(",") if c.strip()}

    def company_allowed(self, company_code: str | None) -> bool:
        allowed = self.allowed_companies
        if not allowed:
            return True
        return (company_code or "").lower() in allowed


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
