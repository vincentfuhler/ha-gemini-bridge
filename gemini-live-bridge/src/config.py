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
    GEMINI_API_KEY: str
    GEMINI_MODEL: str = "models/gemini-2.0-flash-exp" # default model supporting Live API
    GEMINI_VOICE: str = "Puck" # Optional Voice name: Aoede, Charon, Fenrir, Kore, Puck

    HOST: str = "0.0.0.0"
    PORT: int = 8000
    LOG_LEVEL: str = "INFO"

    # Audio details for Home Assistant integration
    # Gemini expects 16kHz PCM audio or similar depending on the exact format provided.
    SAMPLE_RATE: int = 16000
    CHANNELS: int = 1

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()
