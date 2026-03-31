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
            from openwakeword.utils import download_models
            
            logger.info("Checking and downloading core openwakeword models (melspectrogram, etc)...")
            download_models()
            
            target = "computer"
            target_path_local = os.path.join(os.path.dirname(__file__), "..", "..", "wakewords", f"{target}.onnx")
            logger.info(f"Loading bundled openwakeword model: {target_path_local}")
            
            if os.path.exists(target_path_local):
                self.model = Model(wakeword_models=[target_path_local], inference_framework="onnx")
                self.is_loaded = True
                logger.info(f"✅ Wake Word Engine fully loaded: {target}.onnx")
            else:
                logger.error(f"Cannot find bundled openwakeword model at: {target_path_local}")
            
        except ImportError:
            logger.error("openwakeword not installed. Cannot use local wake word detection.")
        except Exception as e:
            logger.error(f"Failed to load Wake Word engine: {e}")
            
    def reset(self):
        """Clears the internal audio feature buffers to prevent ghost activations."""
        if self.is_loaded and self.model:
            try:
                self.model.reset()
                logger.debug("Wake Word model internal buffers reset.")
            except AttributeError:
                pass
            
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
                logger.debug(f"Wake Word Max Score (last 1s): {max_score:.4f}")

        # openwakeword returns prediction scores for all loaded models
        for mdl, score in prediction.items():
            if score > 0.65:  # Trigger threshold (increased to 0.65 to reduce false positives)
                logger.info(f"🎯 WAKE WORD DETECTED! Model: {mdl}, Score: {score}")
                return True
                
        return False

# Global instance
wake_word_engine = WakeWordEngine()
