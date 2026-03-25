from pydantic_settings import BaseSettings, SettingsConfigDict
import os
import json

# Home Assistant Add-on options injection
options_path = "/data/options.json"
if os.path.exists(options_path):
    try:
        with open(options_path, "r") as f:
            options = json.load(f)
            for k, v in options.items():
                os.environ[k] = str(v)
    except Exception as e:
        print(f"Failed to load /data/options.json: {e}")

class Settings(BaseSettings):
    # Gemini
    GEMINI_API_KEY: str
    GEMINI_MODEL: str = "models/gemini-2.5-flash-live-preview"
    GEMINI_VOICE: str = "Puck"  # Aoede, Charon, Fenrir, Kore, Puck

    # Home Assistant API (for function calling)
    HA_URL: str = "http://supervisor/core"   # Default inside HA addon
    HA_TOKEN: str = ""                        # Long-lived access token

    # System prompt file (editable by user in /config/)
    SYSTEM_PROMPT_FILE: str = "/config/gemini_system_prompt.txt"

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    LOG_LEVEL: str = "INFO"

    # Audio
    SAMPLE_RATE: int = 16000
    CHANNELS: int = 1

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def effective_ha_token(self) -> str:
        """Use the internal SUPERVISOR_TOKEN by default for zero-config authentication if the user hasn't explicitly set a valid token."""
        return os.environ.get("SUPERVISOR_TOKEN", "").strip() or self.HA_TOKEN.strip()

settings = Settings()
