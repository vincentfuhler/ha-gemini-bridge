from fastapi import APIRouter
from fastapi.responses import FileResponse
import os

router = APIRouter()

@router.get("/health")
async def health_check():
    """Simple healthcheck endpoint."""
    return {"status": "ok", "service": "ha-gemini-bridge"}

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
