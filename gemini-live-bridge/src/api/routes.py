from fastapi import APIRouter
from fastapi.responses import FileResponse
import os

router = APIRouter()

@router.get("/health")
async def health_check():
    """Simple healthcheck endpoint."""
    return {"status": "ok", "service": "ha-gemini-bridge"}

@router.get("/debug.wav")
async def get_debug_audio():
    """Returns the last recorded debug raw audio from the ESP32 microphone."""
    if os.path.exists("/tmp/debug.wav"):
        return FileResponse("/tmp/debug.wav", media_type="audio/wav")
    return {"error": "No debug audio recorded yet. Try speaking into the microphone first."}
