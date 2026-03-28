from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, RedirectResponse
import os
import logging
from src.core.wakeword import wake_word_engine
from src.config import settings

logger = logging.getLogger("routes")
router = APIRouter()

# ─── Web UI (System Prompt Editor) ───────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def prompt_editor_ui():
    """Serves a web GUI to easily edit the system_prompt.txt"""
    # Prefer settings.SYSTEM_PROMPT_FILE, fallback to ./system_prompt.txt
    file_path = settings.SYSTEM_PROMPT_FILE
    if not os.path.exists(file_path):
        file_path = "/app/system_prompt.txt"
        if not os.path.exists(file_path):
            file_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "system_prompt.txt")

    content = ""
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

    status_message = "Datei erfolgreich geladen."

    html = f"""
    <!DOCTYPE html>
    <html lang="de">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>HA Gemini Bridge - System Prompt</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #1c1c1c; color: #f0f0f0; padding: 20px; }}
            .container {{ max-width: 1000px; margin: 0 auto; background: #2a2a2a; padding: 30px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }}
            h1 {{ border-bottom: 1px solid #444; padding-bottom: 10px; margin-top: 0; }}
            textarea {{ width: 100%; height: 60vh; background: #121212; color: #e0e0e0; border: 1px solid #444; border-radius: 4px; padding: 15px; font-family: monospace; font-size: 14px; box-sizing: border-box; resize: vertical; }}
            button {{ background-color: #03a9f4; color: white; border: none; padding: 12px 24px; font-size: 16px; border-radius: 4px; cursor: pointer; margin-top: 20px; font-weight: bold; }}
            button:hover {{ background-color: #0288d1; }}
            .status {{ color: #4caf50; font-size: 14px; margin-top: 10px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🤖 System Prompt Editor</h1>
            <p>Hier kannst du die Persönlichkeit und Regeln der KI (<strong>{file_path}</strong>) bearbeiten. Änderungen sind sofort für die nächste Sprach-Sitzung aktiv!</p>
            <form action="/api/prompt" method="POST">
                <textarea name="prompt" spellcheck="false">{content}</textarea>
                <div style="display: flex; justify-content: space-between; align-items: baseline;">
                    <button type="submit">💾 System-Prompt Speichern</button>
                    <span class="status">{status_message}</span>
                </div>
            </form>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@router.post("/api/prompt", response_class=RedirectResponse)
async def save_prompt(prompt: str = Form(...)):
    """Saves the submitted text into system_prompt.txt and redirects back to the UI."""
    file_path = settings.SYSTEM_PROMPT_FILE
    # Try to make sure dir exists
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(prompt)
        logger.info(f"System Prompt updated via Web UI ({len(prompt)} chars).")
    except Exception as e:
        logger.error(f"Failed to save system prompt from UI: {e}")

    # Redirect back to the form
    return RedirectResponse(url="/?saved=true", status_code=303)


# ─── Activation State ──────────────────────────────────────────────────────────
# This flag controls whether the bridge forwards ESP32 mic audio to Gemini.
# Toggle via POST /api/activate or /api/deactivate from HA or any HTTP client.
_bridge_active: bool = False


def is_bridge_active() -> bool:
    return _bridge_active

def set_bridge_active(state: bool):
    global _bridge_active
    _bridge_active = state
    if not state:
        wake_word_engine.reset()  # Prevent ghost activations when returning to sleep mode
    logger.info(f"Bridge active state internally set to: {state}")


@router.get("/health")
async def health_check():
    """Simple healthcheck endpoint."""
    return {"status": "ok", "service": "ha-gemini-bridge", "bridge_active": _bridge_active}


@router.post("/api/activate")
async def activate():
    """Activate the bridge — mic audio will be forwarded to Gemini."""
    global _bridge_active
    _bridge_active = True
    logger.info("Bridge ACTIVATED. Mic audio will be forwarded to Gemini.")
    return {"success": True, "bridge_active": True}


@router.post("/api/deactivate")
async def deactivate():
    """Deactivate the bridge — mic audio is received but discarded."""
    global _bridge_active
    _bridge_active = False
    wake_word_engine.reset()  # Prevent ghost activations from stale audio clips
    logger.info("Bridge DEACTIVATED. Mic audio will be discarded.")
    return {"success": True, "bridge_active": False}


@router.get("/api/status")
async def status():
    """Get current activation state."""
    return {"bridge_active": _bridge_active}
