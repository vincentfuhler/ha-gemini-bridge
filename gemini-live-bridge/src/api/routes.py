from fastapi import APIRouter

router = APIRouter()

@router.get("/health")
async def health_check():
    """Simple healthcheck endpoint."""
    return {"status": "ok", "service": "ha-gemini-bridge"}
