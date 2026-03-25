import asyncio
import audioop
import time
import wave
from fastapi import WebSocket
from websockets.exceptions import ConnectionClosed

from src.logging import setup_logger
from src.gemini.client import GeminiLiveClient
from src.api.routes import is_bridge_active, set_bridge_active
from src.core.wakeword import wake_word_engine

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
        
        # Audio parameters from URL query string
        self.in_rate = int(q.get("in_rate", 16000))
        self.in_depth = int(q.get("in_depth", 16))
        self.in_channels = int(q.get("in_channels", 1)) # Added in_channels parsing
        
        self.out_rate = int(q.get("out_rate", 24000))
        self.out_depth = int(q.get("out_depth", 16))
        self.out_channels = int(q.get("out_channels", 1))
        
        # Pacing / Flow Control tracking
        self.bytes_sent_in_turn = 0
        self.turn_start_time = None
        self.last_audio_time = 0.0
        
        # Diagnostic Counters
        self.ha_chunks_received = 0
        self.gemini_chunks_received = 0
        
        # Half-Duplex Echo Prevention:
        # While Gemini is speaking, suppress mic audio to Gemini to prevent self-interruption.
        # The microphone picks up the speaker due to insufficient AEC on the XMOS DSP.
        # speaker_active_until = timestamp until which mic should be suppressed.
        self.speaker_active_until = 0.0
        self.MIC_TAIL_SECS = 1.5  # seconds of suppression AFTER audio stops (echo tail)
        
        # Debugging: Save first 8 seconds of Gemini output
        self.out_wav = wave.open("/tmp/debug_out.wav", "wb")
        self.out_wav.setnchannels(self.out_channels)
        self.out_wav.setsampwidth(self.out_depth // 8)
        self.out_wav.setframerate(self.out_rate)
        self.out_bytes_saved = 0

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
                    self.ha_chunks_received += 1

                    # 1. Hardware DSP (XMOS XU316) provides 32-bit Stereo or similar formats.
                    # Standardize everything to 16kHz 16-bit Mono (required by OpenWakeWord & Gemini bounds)
                    if self.in_channels == 2:
                        pcm_bytes = audioop.tomono(pcm_bytes, self.in_depth // 8, 1.0, 0.0)
                    if self.in_depth != 16:
                        pcm_bytes = audioop.lin2lin(pcm_bytes, self.in_depth // 8, 2)
                    if self.in_rate != 16000:
                        pcm_bytes, _ = audioop.ratecv(pcm_bytes, 2, 1, self.in_rate, 16000, None)

                    # Boost volume by 5.0x (ESP32 I2S mics are notoriously quiet, which starves the Wake Word model)
                    pcm_bytes = audioop.mul(pcm_bytes, 2, 5.0)

                    # 2. Gate: only forward mic audio to Gemini when bridge is active.
                    # If inactive, we use this audio purely for Wake Word detection!
                    if not is_bridge_active():
                        if wake_word_engine.process_chunk(pcm_bytes):
                            logger.info(f"[Session {self.session_id}] 🔔 WAKE WORD DETECTED! Activating bridge.")
                            set_bridge_active(True)
                            await self._play_ding()
                        continue  # Drop chunk from reaching Gemini

                    # 3. Half-Duplex: Suppress mic audio while speaker is playing (+ echo tail)
                    if time.time() < self.speaker_active_until:
                        continue  # Drop this mic chunk — speaker is talking, ignore echo
                        
                    if self.ha_chunks_received == 1:
                        logger.info(f"[Session {self.session_id}] 🎤 First real audio chunk received from ESP32 Microphone!")
                        try:
                            self.debug_wav = wave.open("/tmp/debug.wav", "wb")
                            self.debug_wav.setnchannels(1)
                            self.debug_wav.setsampwidth(2)
                            self.debug_wav.setframerate(16000)
                        except Exception as e:
                            logger.error(f"Failed to open debug.wav: {e}")
                            
                    elif self.ha_chunks_received % 50 == 0:
                        out_volume = audioop.rms(pcm_bytes, 2)
                        logger.info(f"[Session {self.session_id}] 🎤 Forwarded {self.ha_chunks_received} microphone chunks to Gemini... (Mic RMS: {out_volume})")
                        
                    if hasattr(self, "debug_wav") and self.ha_chunks_received <= 500:
                        try:
                            self.debug_wav.writeframes(pcm_bytes)
                            if self.ha_chunks_received == 500:
                                self.debug_wav.close()
                                logger.info(f"[Session {self.session_id}] 💾 Saved 8 seconds of microphone audio to /tmp/debug.wav!")
                        except Exception:
                            pass
                    
                    await self.gemini_client.send_audio_chunk(pcm_bytes)
                elif message.get("text"):
                    # Could handle commands (e.g. JSON metadata) from HA here
                    text_data = message["text"]
                    logger.debug(f"[Session {self.session_id}] Text msg from HA: {text_data}")
                    
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"[Session {self.session_id}] HA disconnected or error: {e}")

    async def _play_ding(self):
        """Play a simple synthetic sine wave 'ding' to acknowledge wake word."""
        try:
            import numpy as np
            # Generate 0.3s of 600Hz + 800Hz sine waves
            duration = 0.3
            t = np.linspace(0, duration, int(16000 * duration), False)
            tone = np.sin(2 * np.pi * 600 * t) + np.sin(2 * np.pi * 800 * t)
            # fade out
            tone = tone * np.linspace(1, 0, len(t))
            audio = np.int16(tone * 10000)
            
            pcm_bytes = audio.tobytes()
            # Send through the same formatting as Gemini audio:
            if getattr(self, "out_rate", 16000) != 16000:
                pcm_bytes, _ = audioop.ratecv(pcm_bytes, 2, 1, 16000, self.out_rate, None)
            if getattr(self, "out_depth", 16) != 16:
                pcm_bytes = audioop.lin2lin(pcm_bytes, 2, self.out_depth // 8)
            if getattr(self, "out_channels", 1) == 2:
                pcm_bytes = audioop.tostereo(pcm_bytes, self.out_depth // 8, 1, 1)
                
            await self.ha_ws.send_bytes(pcm_bytes)
            # Suppress mic while ding is playing
            self.speaker_active_until = time.time() + duration + self.MIC_TAIL_SECS
            logger.info(f"[Session {self.session_id}] 🔔 Chime sent to ESP32.")
        except Exception as e:
            logger.error(f"Failed to play ding: {e}")

    async def _on_gemini_audio_chunk(self, pcm_bytes: bytes):
        """Callback invoked when Gemini produces audio bytes. Sends directly back to HA."""
        try:
            if getattr(self, "out_rate", 16000) != 24000:
                pcm_bytes, _ = audioop.ratecv(pcm_bytes, 2, 1, 24000, self.out_rate, None)
            if getattr(self, "out_depth", 16) != 16:
                pcm_bytes = audioop.lin2lin(pcm_bytes, 2, self.out_depth // 8)
            if getattr(self, "out_channels", 1) == 2:
                pcm_bytes = audioop.tostereo(pcm_bytes, self.out_depth // 8, 1, 1)
                
            if self.out_wav:
                try:
                    self.out_wav.writeframesraw(pcm_bytes)
                    self.out_bytes_saved += len(pcm_bytes)
                    if self.out_bytes_saved >= self.out_rate * (self.out_depth // 8) * self.out_channels * 8:
                        self.out_wav.close()
                        self.out_wav = None
                        logger.info(f"[Session {self.session_id}] 💾 Saved 8 seconds of Gemini output to /tmp/debug_out.wav!")
                except Exception as e:
                    pass
                
            # Track audio session timing for real-time pacing
            now = time.time()
            # Detect new Gemini turn: > 1s silence means a fresh response is starting
            if self.turn_start_time is None or (now - getattr(self, "last_audio_time", 0)) > 1.0:
                self.turn_start_time = now
                self.bytes_sent_in_turn = 0
                logger.info(f"[Session {self.session_id}] New audio turn started.")

            # Mark speaker as active until this chunk finishes + echo tail
            chunk_duration = len(pcm_bytes) / (self.out_rate * (self.out_depth // 8) * self.out_channels)
            self.speaker_active_until = time.time() + chunk_duration + self.MIC_TAIL_SECS
            
            self.last_audio_time = now
            self.bytes_sent_in_turn += len(pcm_bytes)

            # Real-time Pacing (Leaky Bucket):
            # The ESP32 PSRAM buffer is only 96,000 bytes (~0.5s of audio).
            # Gemini sends bursts faster than real-time. We must pace to prevent drops.
            bytes_per_sec = self.out_rate * (self.out_depth // 8) * self.out_channels
            audio_seconds_sent = self.bytes_sent_in_turn / bytes_per_sec
            elapsed_time = time.time() - self.turn_start_time  # Recalculate after potential sleep

            # Keep at most 0.4 seconds ahead of real time
            # This fills the PSRAM buffer comfortably without causing overflow drops.
            # Too low = underruns. Too high = overflow + jumbled audio.
            buffer_ahead_secs = audio_seconds_sent - elapsed_time
            if buffer_ahead_secs > 0.4:
                await asyncio.sleep(buffer_ahead_secs - 0.2)  # Sleep down to 0.2s ahead

            self.gemini_chunks_received += 1
            if self.gemini_chunks_received == 1:
                logger.info(f"[Session {self.session_id}] 🔊 First audio response chunk RECEIVED FROM GEMINI!")
            elif self.gemini_chunks_received % 100 == 0:
                logger.info(f"[Session {self.session_id}] 🔊 Forwarded {self.gemini_chunks_received} audio chunks from Gemini to ESP32...")

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
