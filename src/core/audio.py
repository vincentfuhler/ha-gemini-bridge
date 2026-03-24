"""
Audio formatting and processing utilities.
Home Assistant typically streams PCM 16-bit mono audio at 16kHz.
Gemini Live API expects matching raw PCM audio chunks.
"""

from pydantic import BaseModel

class AudioFormat(BaseModel):
    sample_rate: int = 16000
    channels: int = 1
    sample_width: int = 2 # 16-bit

# Currently Gemini requires raw PCM audio, 16kHz or 24kHz.
# If Home Assistant sends a different format, we could implement
# resampling here (e.g. using audioop or numpy).
# For optimal latency, HA should be configured to send 16kHz raw PCM.
