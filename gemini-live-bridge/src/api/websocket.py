import uuid
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.core.session import Session
from src.core.training import TrainingSession
from src.config import settings
from src.logging import setup_logger

logger = setup_logger("api_websocket")
router = APIRouter()

@router.websocket("/ws")
async def ha_voice_websocket(websocket: WebSocket):
    """
    WebSocket endpoint for Home Assistant Voice integration.
    Expects to receive stream of audio chunks mapping to Gemini Live requirements.
    """
    session_id = str(uuid.uuid4())
    logger.info(f"New WebSocket connection requested. Session ID: {session_id}")
    
    if settings.TRAINING_MODE:
        session = TrainingSession(websocket, session_id)
        logger.info(f"Starting TRAINING Session {session_id}")
    else:
        session = Session(websocket, session_id)
        
    try:
        result = await session.start()
        if result == "SWITCH_TO_TRAINING":
            logger.info(f"🔄 Dynamically switching connection {session_id} to Training Mode!")
            t_session = TrainingSession(websocket, session_id)
            await t_session.start()
    except WebSocketDisconnect:
        logger.info(f"Client disconnected gracefully: {session_id}")
    except Exception as e:
        logger.error(f"Error in overall session {session_id}: {e}")
    finally:
        await session.cleanup()
        logger.info(f"Session {session_id} ended.")
