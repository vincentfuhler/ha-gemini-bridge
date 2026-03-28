from fastapi import FastAPI
from contextlib import asynccontextmanager
from src.api import routes, websocket
from src.config import settings
from src.core.wakeword import wake_word_engine
from fastapi.staticfiles import StaticFiles
from src.logging import setup_logger
from src.core.routines import routine_engine
from src.core.optimizer import optimizer_service

import asyncio
from src.ha.events import ha_websocket_listener

logger = setup_logger("main")

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting HA Gemini Bridge...")
    # Load the Wake Word model synchronously on startup (to prevent timeouts on first audio interaction)
    wake_word_engine.load()
    
    optimizer_service.start()
    
    # Start the Agentic Routine HA Event Listener
    event_task = asyncio.create_task(ha_websocket_listener())
    
    yield
    
    logger.info("Shutting down HA Gemini Bridge...")
    await optimizer_service.stop()
    
    event_task.cancel()
app = FastAPI(
    title="HA Gemini Bridge",
    description="Low-Latency Bridge for Home Assistant Voice to Gemini Live API",
    version="1.4.4",
    lifespan=lifespan
)

# Include API Routers
app.include_router(routes.router)
app.include_router(websocket.router)

# Serve wakewords statically for ESPHome
import os
wakewords_dir = os.path.join(os.path.dirname(__file__), "..", "wakewords")
if os.path.exists(wakewords_dir):
    app.mount("/wakewords", StaticFiles(directory=wakewords_dir), name="wakewords")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "src.main:app", 
        host=settings.HOST, 
        port=settings.PORT, 
        log_level=settings.LOG_LEVEL.lower()
    )
