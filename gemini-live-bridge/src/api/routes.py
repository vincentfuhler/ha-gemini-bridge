from fastapi import APIRouter
import os
import logging
from src.core.wakeword import wake_word_engine

logger = logging.getLogger("routes")
router = APIRouter()

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
