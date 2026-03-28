import asyncio
import audioop
import time
import wave
from fastapi import WebSocket
from websockets.exceptions import ConnectionClosed

from src.logging import setup_logger
from src.gemini.client import GeminiLiveClient
from src.core.wakeword import wake_word_engine

logger = setup_logger("session_manager")

class Session:
    """
    Manages a single conversation session between a Home Assistant client and Gemini.
    """
    def __init__(self, websocket: WebSocket, session_id: str):
        self.ha_ws = websocket
        self.session_id = session_id
        
        self.is_active = False
        self.gemini_task = None
        self.watchdog_task = None
        
        self.gemini_client = GeminiLiveClient()
        self.gemini_client.on_conversation_end = self.deactivate
        
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
        self.last_speaker_time = 0.0   # Gemini's pacing
        self.last_audio_time = 0.0     # System watchdog
        
        # Diagnostic Counters
        self.ha_chunks_received = 0
        self.gemini_chunks_received = 0
        
        # Half-Duplex Echo Prevention:
        # While Gemini is speaking, suppress mic audio to Gemini to prevent self-interruption.
        # The microphone picks up the speaker due to insufficient AEC on the XMOS DSP.
        # speaker_active_until = timestamp until which mic should be suppressed.
        self.speaker_active_until = 0.0
        self.MIC_TAIL_SECS = 1.5  # seconds of suppression AFTER audio stops (echo tail)

        # Pre-buffer for Wake Word context (1.5 seconds of 16kHz 16-bit Mono = 48000 bytes)
        self.pre_buffer = bytearray()
        self.PRE_BUFFER_SIZE = 48000

    async def start(self):
        """Starts the session by connecting to Gemini and beginning the duplex stream."""
        await self.ha_ws.accept()
        logger.info(f"[Session {self.session_id}] Intercom started. Waiting for wake word.")
        try:
            await self.ha_ws.send_text('{"state": "connected"}')
        except Exception:
            pass

        # The session lives as long as the HA WebSocket is alive
        try:
            await self._ha_to_gemini_loop()
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

                    # 2. Half-Duplex Echo Prevention: Suppress ALL mic audio 
                    # while the speaker is playing and during the echo tail so it doesn't trigger wake words.
                    if time.time() < self.speaker_active_until:
                        continue  # Drop this mic chunk completely!

                    # 3. Gate: only forward mic audio to Gemini when bridge is active.
                    # If inactive, we use this audio purely for Wake Word detection!
                    if not self.is_active:
                        self.pre_buffer.extend(pcm_bytes)
                        if len(self.pre_buffer) > self.PRE_BUFFER_SIZE:
                            # Keep only the latest PRE_BUFFER_SIZE bytes
                            self.pre_buffer = self.pre_buffer[-self.PRE_BUFFER_SIZE:]

                        if wake_word_engine.process_chunk(pcm_bytes):
                            asyncio.create_task(self.activate())
                        continue  # Drop chunk from reaching Gemini
                        
                    if self.ha_chunks_received == 1:
                        logger.info(f"[Session {self.session_id}] 🎤 First real audio chunk received from ESP32 Microphone!")
                    elif self.ha_chunks_received % 50 == 0:
                        out_volume = audioop.rms(pcm_bytes, 2)
                        logger.debug(f"[Session {self.session_id}] 🎤 Forwarded {self.ha_chunks_received} microphone chunks to Gemini... (Mic RMS: {out_volume})")
                        
                    if self.gemini_client.ws:
                        # Reset idle timer if user is actively speaking (RMS > 800)
                        if self.ha_chunks_received % 10 == 0:
                            if audioop.rms(pcm_bytes, 2) > 800:
                                self.last_audio_time = time.time()
                                self.timeout_prompt_sent = False
                                
                        # Do not send realtime audio if we just sent a turnComplete text message (prevents 1008 error)
                        if not getattr(self, "timeout_prompt_sent", False):
                            await self.gemini_client.send_audio_chunk(pcm_bytes)
                elif message.get("text"):
                    # Could handle commands (e.g. JSON metadata) from HA here
                    text_data = message["text"]
                    logger.debug(f"[Session {self.session_id}] Text msg from HA: {text_data}")
                    
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"[Session {self.session_id}] HA disconnected or error: {e}")

    def deactivate(self):
        self.is_active = False
        wake_word_engine.reset()
        logger.info(f"[Session {self.session_id}] Deactivated. Gemini disconnected. Waiting for Wake Word.")
        try:
            asyncio.create_task(self.ha_ws.send_text('{"state": "connected"}'))
        except Exception:
            pass
            
        if self.gemini_client.ws:
            asyncio.create_task(self.gemini_client.close())
        if self.gemini_task:
            self.gemini_task.cancel()
            self.gemini_task = None
        if self.watchdog_task:
            self.watchdog_task.cancel()
            self.watchdog_task = None
            
    async def _run_gemini_task(self):
        """Wrapper to run the Gemini receive loop and deactivate on exit or crash."""
        try:
            await self.gemini_client.receive_loop(self._on_gemini_audio_chunk)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[Session {self.session_id}] Gemini receive task failed: {e}")
        finally:
            if self.is_active:
                logger.warning(f"[Session {self.session_id}] Gemini loop ended unexpectedly. Deactivating.")
                self.deactivate()

    async def activate(self):
        if self.is_active: return
        self.is_active = True
        logger.info(f"[Session {self.session_id}] 🔔 WAKE WORD DETECTED! Activating bridge.")
        try:
            await self.ha_ws.send_text('{"state": "listening"}')
        except Exception:
            pass
        await self._play_ding()
        
        logger.info(f"[Session {self.session_id}] 🚀 Connecting to Gemini Live API...")
        try:
            await self.gemini_client.connect()

            # 🚀 IMMEDIATELY flush the pre-buffer containing the Wake Word to Gemini 🚀
            if self.pre_buffer:
                logger.info(f"[Session {self.session_id}] 🔙 Flushing {len(self.pre_buffer)} bytes of wake-word context to Gemini.")
                await self.gemini_client.send_audio_chunk(bytes(self.pre_buffer))
                self.pre_buffer.clear()

            self.gemini_task = asyncio.create_task(self._run_gemini_task())
            self.tasks.append(self.gemini_task)
            
            # Start Watchdog
            self.last_audio_time = time.time()
            self.timeout_prompt_sent = False
            self.watchdog_task = asyncio.create_task(self._inactivity_watchdog())
            self.tasks.append(self.watchdog_task)
            
            logger.info(f"[Session {self.session_id}] 🟢 Gemini Connection established!")
        except Exception as e:
            logger.error(f"Failed to connect to Gemini: {e}")
            self.deactivate()

    async def _inactivity_watchdog(self):
        """Monitors session for silence and prompts Gemini to cleanly exit if forgotten."""
        try:
            while self.is_active:
                await asyncio.sleep(2)
                if not self.is_active:
                    break
                    
                idle_time = time.time() - self.last_audio_time
                if idle_time > 15.0 and not getattr(self, "timeout_prompt_sent", False):
                    self.timeout_prompt_sent = True
                    logger.info(f"[Session {self.session_id}] ⏱️ Session idle for 15s. Prompting Gemini to close if done.")
                    if self.gemini_client.ws:
                        msg = "Systemhinweis: Seit 15 Sekunden gab es keine Aktivität. Die Konversation ist beendet. Rufe jetzt sofort 'end_conversation' auf."
                        await self.gemini_client.send_text(msg)
                        
                # Hard fallback: if 30s pass, kill it anyway
                if idle_time > 30.0:
                    logger.warning(f"[Session {self.session_id}] ⏱️ Hard timeout reached (30s). Terminating session.")
                    self.deactivate()
                    break
        except asyncio.CancelledError:
            pass

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
                
            # Track audio session timing for real-time pacing
            now = time.time()
            # Detect new Gemini turn: > 1s silence means a fresh response is starting
            if self.turn_start_time is None or (now - getattr(self, "last_speaker_time", 0)) > 1.0:
                self.turn_start_time = now
                self.bytes_sent_in_turn = 0
                self.timeout_prompt_sent = False  # Resume mic forwarding if it was blocked by watchdog
                logger.info(f"[Session {self.session_id}] New audio turn started.")

            # Mark speaker as active until this chunk finishes + echo tail
            chunk_duration = len(pcm_bytes) / (self.out_rate * (self.out_depth // 8) * self.out_channels)
            self.speaker_active_until = time.time() + chunk_duration + self.MIC_TAIL_SECS
            
            self.last_speaker_time = now
            self.last_audio_time = now
            self.bytes_sent_in_turn += len(pcm_bytes)

            # Real-time Pacing (Leaky Bucket):
            # The ESP32 PSRAM buffer is only 96,000 bytes (~0.5s of audio).
            # Gemini sends bursts faster than real-time. We must pace to prevent drops.
            bytes_per_sec = self.out_rate * (self.out_depth // 8) * self.out_channels
            audio_seconds_sent = self.bytes_sent_in_turn / bytes_per_sec
            elapsed_time = time.time() - self.turn_start_time  # Recalculate after potential sleep

            # Keep at most 0.8 seconds ahead of real time
            # This fills the PSRAM buffer (which is 192,000 bytes / 1.0s at 48kHz stereo) comfortably.
            # Too low = underruns (stuttering). Too high = overflow + jumbled audio.
            buffer_ahead_secs = audio_seconds_sent - elapsed_time
            if buffer_ahead_secs > 0.8:
                await asyncio.sleep(buffer_ahead_secs - 0.5)  # Sleep down to 0.5s ahead

            self.gemini_chunks_received += 1
            if self.gemini_chunks_received == 1:
                logger.info(f"[Session {self.session_id}] 🔊 First audio response chunk RECEIVED FROM GEMINI!")
            elif self.gemini_chunks_received % 100 == 0:
                logger.debug(f"[Session {self.session_id}] 🔊 Forwarded {self.gemini_chunks_received} audio chunks from Gemini to ESP32...")

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
