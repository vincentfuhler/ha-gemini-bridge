from fastapi import APIRouter
from fastapi.responses import FileResponse
import os
import logging

logger = logging.getLogger("routes")
router = APIRouter()

# ─── Activation State ──────────────────────────────────────────────────────────
# This flag controls whether the bridge forwards ESP32 mic audio to Gemini.
# Toggle via POST /api/activate or /api/deactivate from HA or any HTTP client.
_bridge_active: bool = False


def is_bridge_active() -> bool:
    return _bridge_active


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
    logger.info("Bridge DEACTIVATED. Mic audio will be discarded.")
    return {"success": True, "bridge_active": False}


@router.get("/api/status")
async def status():
    """Get current activation state."""
    return {"bridge_active": _bridge_active}


@router.get("/debug.wav")
async def download_debug_wav():
    """Download the debug audio file if it exists."""
    file_path = "/tmp/debug.wav"
    if os.path.exists(file_path):
        return FileResponse(file_path, media_type="audio/wav", filename="debug.wav")
    return {"error": "No debug.wav found. Start a session and speak into the mic first!"}


@router.get("/debug_out.wav")
async def download_debug_out_wav():
    """Download the debug Gemini response audio file if it exists."""
    file_path = "/tmp/debug_out.wav"
    if os.path.exists(file_path):
        return FileResponse(file_path, media_type="audio/wav", filename="debug_out.wav")
    return {"error": "No debug_out.wav found. Start a session and let Gemini speak first!"}
