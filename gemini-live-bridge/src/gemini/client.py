import asyncio
import base64
import json
import websockets
from typing import AsyncGenerator, Callable, Any

from src.logging import setup_logger
from src.config import settings

logger = setup_logger("gemini_client")

class GeminiLiveClient:
    """
    Client for interacting with the Gemini Live / Multimodal API via WebSockets.
    """
    def __init__(self):
        self.api_key = settings.GEMINI_API_KEY
        self.model = settings.GEMINI_MODEL
        self.voice = settings.GEMINI_VOICE
        self.ws: websockets.WebSocketClientProtocol | None = None
        self.uri = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={self.api_key}"

    async def connect(self):
        """Establish the WebSocket connection to Gemini."""
        logger.info(f"Connecting to Gemini Live API with model {self.model}...")
        self.ws = await websockets.connect(self.uri)
        
        # Send initial setup message
        setup_msg = {
            "setup": {
                "model": self.model,
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {
                            "prebuiltVoiceConfig": {
                                "voiceName": self.voice
                            }
                        }
                    }
                }
            }
        }
        await self.ws.send(json.dumps(setup_msg))
        
        # Wait for setup completion
        response_raw = await self.ws.recv()
        response = json.loads(response_raw)
        if "setupComplete" in response:
            logger.info("Gemini setup completed successfully.")
        else:
            logger.error(f"Failed to complete setup. Response: {response}")
            raise Exception("Initial setup for Gemini Live failed.")

    async def send_audio_chunk(self, pcm_bytes: bytes):
        """Send a chunk of raw PCM audio to Gemini."""
        if not self.ws:
            return

        b64_audio = base64.b64encode(pcm_bytes).decode("utf-8")
        msg = {
            "realtimeInput": {
                "mediaChunks": [
                    {
                        "mimeType": "audio/pcm;rate=16000",
                        "data": b64_audio
                    }
                ]
            }
        }
        await self.ws.send(json.dumps(msg))

    async def send_text(self, text: str):
        """Send a text input message if needed."""
        if not self.ws:
            return
            
        msg = {
            "clientContent": {
                "turns": [
                    {
                        "role": "user",
                        "parts": [{"text": text}]
                    }
                ],
                "turnComplete": True
            }
        }
        await self.ws.send(json.dumps(msg))

    async def receive_loop(self, on_audio_chunk: Callable[[bytes], Any]):
        """
        Continuously listen for responses from Gemini.
        `on_audio_chunk` is a callback function called when audio data is received.
        """
        if not self.ws:
            return

        try:
            async for message in self.ws:
                data = json.loads(message)
                
                # Check for serverContent -> modelTurn
                if "serverContent" in data:
                    model_turn = data["serverContent"].get("modelTurn")
                    if model_turn:
                        for part in model_turn.get("parts", []):
                            # Check for text part (could also be used for logs/UI)
                            if "text" in part:
                                logger.debug(f"Gemini text chunk: {part['text']}")
                            
                            # Check for audio part
                            if "inlineData" in part:
                                mime_type = part["inlineData"].get("mimeType", "")
                                if mime_type.startswith("audio/pcm"):
                                    b64_data = part["inlineData"]["data"]
                                    pcm_bytes = base64.b64decode(b64_data)
                                    # Call the callback to push the chunk back to Home Assistant
                                    if asyncio.iscoroutinefunction(on_audio_chunk):
                                        await on_audio_chunk(pcm_bytes)
                                    else:
                                        on_audio_chunk(pcm_bytes)
                                        
                    # Determine if turn is complete
                    if data["serverContent"].get("turnComplete"):
                        logger.debug("Gemini turn complete.")

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"Gemini connection closed: {e}")
        except Exception as e:
            logger.error(f"Error in Gemini receive loop: {e}")

    async def close(self):
        """Close the WebSocket connection."""
        if self.ws:
            await self.ws.close()
            self.ws = None
            logger.info("Disconnected from Gemini.")
