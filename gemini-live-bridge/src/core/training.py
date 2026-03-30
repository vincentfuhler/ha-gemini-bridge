import asyncio
import audioop
import time
import wave
import os
import zipfile
import uuid
import numpy as np
from fastapi import WebSocket
from websockets.exceptions import ConnectionClosed

from src.logging import setup_logger
from src.config import settings

logger = setup_logger("training_session")

TRAINING_DIR = "/data/training_data"
ZIP_FILE_PATH = "/data/training_data.zip"

class TrainingSession:
    """
    Spezielle Session für die Erstellung von Wake-Word Trainingsdaten.
    """
    def __init__(self, websocket: WebSocket, session_id: str):
        self.ha_ws = websocket
        self.session_id = session_id
        
        q = websocket.query_params
        self.in_rate = int(q.get("in_rate", 16000))
        self.in_depth = int(q.get("in_depth", 16))
        self.in_channels = int(q.get("in_channels", 1))
        
        self.out_rate = int(q.get("out_rate", 24000))
        self.out_depth = int(q.get("out_depth", 16))
        self.out_channels = int(q.get("out_channels", 1))

        self.TARGET_BYTES = 3 * 16000 * 2  # 3 seconds * 16000 samples/sec * 2 bytes/sample

    async def start(self, already_accepted: bool = False):
        if not already_accepted:
            await self.ha_ws.accept()
        logger.info(f"[Training {self.session_id}] Training mode gestartet.")
        os.makedirs(TRAINING_DIR, exist_ok=True)
        
        try:
            # Idle Status schicken
            await self.ha_ws.send_text('{"state": "listening"}')
            await asyncio.sleep(2)  # Kurze Pause nach dem Verbinden
            
            # 1. 100x Positiv
            logger.info(f"[Training {self.session_id}] Starte 100x Positiv-Aufnahmen.")
            for i in range(1, 101):
                await self._play_pings(1)
                await asyncio.sleep(0.5) # Warte kurz auf den Speaker
                
                audio_data = await self._record_seconds(3.0)
                file_path = os.path.join(TRAINING_DIR, f"{i}_postiv.wav")
                self._save_wav(audio_data, file_path)
                logger.debug(f"Positiv {i}/100 gespeichert.")

            # 2. 3 Pings vor Negativ
            logger.info(f"[Training {self.session_id}] 3 Pings als Trennsignal.")
            await self._play_pings(3)
            await asyncio.sleep(1.0)
            
            # 3. 100x Negativ (ohne Pings dazwischen)
            logger.info(f"[Training {self.session_id}] Starte 100x Negativ-Aufnahmen nahtlos.")
            for i in range(1, 101):
                audio_data = await self._record_seconds(3.0)
                file_path = os.path.join(TRAINING_DIR, f"{i}_negativ.wav")
                self._save_wav(audio_data, file_path)
                logger.debug(f"Negativ {i}/100 gespeichert.")

            # 4. 5 Pings als Abschluss
            logger.info(f"[Training {self.session_id}] 5 Pings als Abschluss.")
            await self._play_pings(5)
            
            # 5. ZIP erstellen
            self._create_zip()
            
            # Fertig status
            await self.ha_ws.send_text('{"state": "idle"}')
            logger.info(f"[Training {self.session_id}] Trainingsmodus erfolgreich abgeschlossen!")
            
            # Session weiter offen halten, damit er nicht rebooted
            while True:
                await self.ha_ws.receive()
                
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[Training {self.session_id}] Fehler im Training: {e}")
        finally:
            await self.cleanup()

    async def _record_seconds(self, seconds: float) -> bytearray:
        """Sammelt Audio Daten bis die gewünschte Länge erreicht ist (in 16kHz, 16-bit Mono)."""
        buffer = bytearray()
        target_bytes = int(seconds * 16000 * 2)
        
        while len(buffer) < target_bytes:
            message = await self.ha_ws.receive()
            if message.get("bytes"):
                pcm_bytes = message["bytes"]
                
                # Format anpassen
                if self.in_channels == 2:
                    pcm_bytes = audioop.tomono(pcm_bytes, self.in_depth // 8, 1.0, 0.0)
                if self.in_depth != 16:
                    pcm_bytes = audioop.lin2lin(pcm_bytes, self.in_depth // 8, 2)
                if self.in_rate != 16000:
                    pcm_bytes, _ = audioop.ratecv(pcm_bytes, 2, 1, self.in_rate, 16000, None)
                    
                buffer.extend(pcm_bytes)
                
        # Exakt auf Ziel-Größe abschneiden (falls zu viel)
        return buffer[:target_bytes]

    def _save_wav(self, audio_data: bytearray, file_path: str):
        with wave.open(file_path, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(audio_data)

    def _create_zip(self):
        logger.info(f"[Training {self.session_id}] Erstelle ZIP Archiv...")
        with zipfile.ZipFile(ZIP_FILE_PATH, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, _, files in os.walk(TRAINING_DIR):
                for file in files:
                    if file.endswith('.wav'):
                        file_path = os.path.join(root, file)
                        zipf.write(file_path, arcname=file)
                        os.remove(file_path) # Aufräumen der einzelnen Files nach dem Zippen
        logger.info(f"[Training {self.session_id}] ZIP Archiv bereit: {ZIP_FILE_PATH}")

    async def _play_pings(self, count: int):
        duration = 0.3
        t = np.linspace(0, duration, int(16000 * duration), False)
        tone = np.sin(2 * np.pi * 600 * t) + np.sin(2 * np.pi * 800 * t)
        tone = tone * np.linspace(1, 0, len(t))
        audio = np.int16(tone * 10000)
        pcm_bytes = audio.tobytes()
        
        if getattr(self, "out_rate", 16000) != 16000:
            pcm_bytes, _ = audioop.ratecv(pcm_bytes, 2, 1, 16000, self.out_rate, None)
        if getattr(self, "out_depth", 16) != 16:
            pcm_bytes = audioop.lin2lin(pcm_bytes, 2, self.out_depth // 8)
        if getattr(self, "out_channels", 1) == 2:
            pcm_bytes = audioop.tostereo(pcm_bytes, self.out_depth // 8, 1, 1)

        for _ in range(count):
            await self.ha_ws.send_bytes(pcm_bytes)
            await asyncio.sleep(0.5)

    async def cleanup(self):
        try:
            await self.ha_ws.close()
        except Exception:
            pass
