from fastapi import FastAPI
from src.api import routes, websocket
from src.config import settings

app = FastAPI(
    title="HA Gemini Bridge",
    description="Low-Latency Bridge for Home Assistant Voice to Gemini Live API",
    version="1.0.44"
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
