from fastapi import FastAPI
from contextlib import asynccontextmanager
from src.api import routes, websocket
from src.config import settings
from src.core.wakeword import wake_word_engine

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load the Wake Word model synchronously on startup (to prevent timeouts on first audio interaction)
    wake_word_engine.load()
    yield

app = FastAPI(
    title="HA Gemini Bridge",
    description="Low-Latency Bridge for Home Assistant Voice to Gemini Live API",
    version="1.2.1",
    lifespan=lifespan
)

# Include API Routers
app.include_router(routes.router)
app.include_router(websocket.router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "src.main:app", 
        host=settings.HOST, 
        port=settings.PORT, 
        log_level=settings.LOG_LEVEL.lower()
    )
