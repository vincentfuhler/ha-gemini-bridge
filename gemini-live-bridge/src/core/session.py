import asyncio
import audioop
from fastapi import WebSocket
from websockets.exceptions import ConnectionClosed

from src.logging import setup_logger
from src.gemini.client import GeminiLiveClient

logger = setup_logger("session_manager")

class Session:
    """
    Manages a single conversation session between a Home Assistant client and Gemini.
    """
    def __init__(self, websocket: WebSocket, session_id: str):
        self.ha_ws = websocket
        self.session_id = session_id
        self.gemini_client = GeminiLiveClient()
        self.tasks: list[asyncio.Task] = []
        
        q = websocket.query_params
        self.in_rate = int(q.get("in_rate", 16000))
        self.in_depth = int(q.get("in_depth", 16))
        self.out_rate = int(q.get("out_rate", 24000))
        self.out_depth = int(q.get("out_depth", 16))
        self.out_channels = int(q.get("out_channels", 1))


    async def start(self):
        """Starts the session by connecting to Gemini and beginning the duplex stream."""
        await self.ha_ws.accept()
        logger.info(f"[Session {self.session_id}] Intercom started.")

        try:
            await self.gemini_client.connect()
        except Exception as e:
            logger.error(f"[Session {self.session_id}] Failed to connect to Gemini: {e}")
            await self.ha_ws.close(code=1011, reason="Gemini Connection Failed")
            return

        # Start concurrent tasks for full-duplex communication
        # 1. Listen to Home Assistant -> Send to Gemini
        # 2. Listen to Gemini -> Send to Home Assistant
        ha_to_gemini_task = asyncio.create_task(self._ha_to_gemini_loop())
        gemini_to_ha_task = asyncio.create_task(
            self.gemini_client.receive_loop(self._on_gemini_audio_chunk)
        )
        
        self.tasks.extend([ha_to_gemini_task, gemini_to_ha_task])

        # Wait until both tasks finish (or one fails)
        try:
            # return_when=asyncio.FIRST_COMPLETED to stop if one connection drops
            done, pending = await asyncio.wait(
                self.tasks, 
                return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
        except Exception as e:
            logger.error(f"[Session {self.session_id}] Task error: {e}")
        finally:
            await self.cleanup()

    async def _ha_to_gemini_loop(self):
        """Reads audio chunks from Home Assistant and forwards them to Gemini."""
        try:
            while True:
                # Based on HA format, it might be text or bytes. 
                # Assuming raw binary audio frames for this implementation.
                message = await self.ha_ws.receive()
                
                if message.get("bytes"):
                    pcm_bytes = message["bytes"]
                    if self.in_depth != 16:
                        pcm_bytes = audioop.lin2lin(pcm_bytes, self.in_depth // 8, 2)
                    if self.in_rate != 16000:
                        pcm_bytes, _ = audioop.ratecv(pcm_bytes, 2, 1, self.in_rate, 16000, None)
                    
                    await self.gemini_client.send_audio_chunk(pcm_bytes)
                elif message.get("text"):
                    # Could handle commands (e.g. JSON metadata) from HA here
                    text_data = message["text"]
                    logger.debug(f"[Session {self.session_id}] Text msg from HA: {text_data}")
                    
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"[Session {self.session_id}] HA disconnected or error: {e}")

    async def _on_gemini_audio_chunk(self, pcm_bytes: bytes):
        """Callback invoked when Gemini produces audio bytes. Sends directly back to HA."""
        try:
            if getattr(self, "out_rate", 16000) != 24000:
                pcm_bytes, _ = audioop.ratecv(pcm_bytes, 2, 1, 24000, self.out_rate, None)
            if getattr(self, "out_depth", 16) != 16:
                pcm_bytes = audioop.lin2lin(pcm_bytes, 2, self.out_depth // 8)
            if getattr(self, "out_channels", 1) == 2:
                pcm_bytes = audioop.tostereo(pcm_bytes, self.out_depth // 8, 1, 1)
                
            await self.ha_ws.send_bytes(pcm_bytes)
        except Exception as e:
            logger.error(f"[Session {self.session_id}] Error sending to HA: {e}")
            raise  # Will propagate and cancel loops

    async def cleanup(self):
        """Cleans up sockets and tasks."""
        logger.info(f"[Session {self.session_id}] Cleaning up session.")
        for task in self.tasks:
            if not task.done():
                task.cancel()
                
        await self.gemini_client.close()
        
        try:
            await self.ha_ws.close()
        except Exception:
            pass
