import os
import logging
import numpy as np
from src.config import settings

logger = logging.getLogger("wakeword")

class WakeWordEngine:
    """
    Singleton engine to run OpenWakeWord natively in the Add-on container.
    """
    def __init__(self):
        self.model = None
        self.is_loaded = False
        
    def load(self):
        if self.is_loaded:
            return
            
        try:
            from openwakeword.model import Model
            
            target = settings.WAKE_WORD
            logger.info(f"Loading openwakeword model: {target}")
            
            # Check if the user has a custom model in /config/wakewords/
            target_path = os.path.join(settings.CUSTOM_WAKE_WORD_DIR, f"{target}.tflite")
            if os.path.exists(target_path):
                logger.info(f"Found custom tflite model at {target_path}")
                self.model = Model(wakeword_models=[target_path], inference_framework="tflite")
            else:
                logger.info(f"Using built-in openwakeword model: {target}")
                self.model = Model(wakeword_models=[target], inference_framework="onnx")
                
            self.is_loaded = True
            logger.info(f"✅ Wake Word Engine fully loaded: {target}")
            
        except ImportError:
            logger.error("openwakeword not installed. Cannot use local wake word detection.")
        except Exception as e:
            logger.error(f"Failed to load Wake Word engine: {e}")
            
    def process_chunk(self, pcm_data: bytes) -> bool:
        """
        Processes a chunk of 16kHz 16-bit mono audio.
        Returns True if the wake word was detected in this chunk.
        """
        if not self.is_loaded or not self.model:
            return False
            
        # openwakeword requires 16000Hz 16-bit mono
        audio_array = np.frombuffer(pcm_data, dtype=np.int16)
        
        prediction = self.model.predict(audio_array)
        
        # Verbose logging of the max score every ~1 second (assuming ~30 chunks per sec)
        self.chunk_count = getattr(self, "chunk_count", 0) + 1
        if self.chunk_count % 30 == 0:
            max_score = max(prediction.values()) if prediction else 0.0
            if max_score > 0.01:
                logger.info(f"Wake Word Max Score (last 1s): {max_score:.4f}")

        # openwakeword returns prediction scores for all loaded models
        for mdl, score in prediction.items():
            if score > 0.5:  # Trigger threshold
                logger.info(f"🎯 WAKE WORD DETECTED! Model: {mdl}, Score: {score}")
                return True
                
        return False

# Global instance
wake_word_engine = WakeWordEngine()
