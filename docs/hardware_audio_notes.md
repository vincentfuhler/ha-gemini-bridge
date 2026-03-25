# Home Assistant Voice PE - Gemini Live Bridge 
**Technical Documentation & Hardware Analysis**

## 1. Hardware Architecture (Home Assistant Voice PE)

The HA Voice Preview Edition uses a rigorous audio hardware pipeline centered around an ESP32-S3 module, which interfaces via I2S with a dedicated DAC out and a Microphone DSP input.

### Key Audio Pins and Specifications:
- **MCU:** ESP32-S3 (WROOM-1)
- **I2S Audio Speaker (DAC Output):**
  - **Pins:** `LRCLK = GPIO39`, `BCLK = GPIO40`, `DOUT = GPIO41`
  - **Driver Constraint:** Highly restrictive `32-bit` bit depth (imposed by the hardware DAC configuration in ESP-IDF).
- **I2S Microphones (Input):**
  - **Pins:** `LRCLK = GPIO45`, `BCLK = GPIO47`, `DIN = GPIO46`
  - **Constraint:** `32-bit` Stereo input. The Left channel contains the actual voice recording, while the Right channel contains the physical acoustic echo cancellation (AEC) reference.

---

## 2. Experimental Approaches to Audio Injection

The core difficulty in bridging Gemini Live directly onto the Voice PE is bypassing the rigid `voice_assistant` State Machine in standard ESPHome while preventing buffer underruns and driver crashes.

Here is the chronology of approaches we attempted:

### Attempt 1: Standard Mono Injection (16Khz 16-bit)
- **Method:** Sending native Gemini mono output directly into the standard ESPHome `resampler`.
- **Result:** **Failed.** The internal `resampler` block in ESPHome is mathematically incapable of upmixing Mono to Stereo. This resulted in severely garbled, silent, or "chipmunk" speed audio.

### Attempt 2: Bypassing the Resampler (Direct Native Mixer Feed)
- **Method:** Passing the upmixing responsibility to our Python Bridge (`session.py`) to convert Gemini's 24kHz Mono directly to 48kHz 16-bit Stereo using `audioop`. We injected this straight into the `media_mixing_input` channel inside ESPHome to bypass the resampler.
- **Result:** **Silent Overflow.** The ESP32 received the chunks perfectly, but the audio never played.
- **Analysis:** ESPHome's internal hardware components (`mixing_speaker` and `i2s_audio_speaker`) aggressively go into a deep `STOPPED` power-saving state immediately after the startup boot-chime plays. Our custom C++ component was throwing audio into a dead queue.

### Attempt 3: The "Wake-Up Call" (Forcing Driver Starts)
- **Method:** Added explicit C++ logic in our `GeminiWebSocketClient` to forcefully run `this->i2s_audio_speaker->start()` if the DAC had gone to sleep.
- **Result:** **ESP-IDF Driver Crash.** 
  - `[E][i2s_audio.speaker:505]: Audio stream settings are not compatible with this I2S configuration`
  - `[E][i2s_audio.speaker:148]: Driver failed to start`
- **Analysis:** When we told the `media_mixing_input` that our incoming Bridge audio was 16-bit (`set_audio_stream_info(16, 2, ...)`), ESPHome cascaded this configuration down to the hardware I2S DMA engine. Because the Voice PE's DAC *stricktly* requires 32-bit samples, the ESP-IDF fundamentally rejected the 16-bit reconfiguration and halted the entire audio subsystem. 

### Attempt 4 (Current Working Strategy): True 32-Bit Python Upscaling
- **Method:** Shifting the 32-bit heavy-lifting entirely to the FastAPI server. Python converts `16-bit -> 32-bit` via `audioop.lin2lin` dynamically. The WebSocket payload requested is `out_depth=32`, and the C++ node correctly configures `audio::AudioStreamInfo info(32, 2, 48000)`.
- **Result:** The exact parameters match the Voice PE's hardware constraints. The ESP-IDF accepts the data, wakes up from sleep successfully, and the DMA flushes correctly to the speaker.

---

## 3. The Functional Audio Data Flow

Below is the highly optimized technical path from Gemini's Cloud WebSocket to the physical speaker cone on the HA Voice PE.

### ⬇️ Receiving Audio (Downstream)
1. **Gemini Live API:** Sends binary audio frame: `16-bit PCM`, `24000 Hz`, `Mono`.
2. **FastAPI Python Bridge (`session.py`):**
   - **Resamples** from `24,000Hz -> 48,000Hz`.
   - **Upmixes** from `Mono -> Stereo` (channel duplication).
   - **Upscales Depth** from `16-bit -> 32-bit` (`audioop.lin2lin`).
3. **C++ `GeminiWebSocketClient` (`gemini_websocket.h`):**
   - Receives chunked packets and places them lock-safely into a massive **96,000-byte PSRAM Ringbuffer**.
   - If the `i2s_audio_speaker` DMA driver is asleep, it issues an immediate `start()` command.
4. **ESPHome `media_mixing_input` & `mixing_speaker`:**
   - ESPHome continuously pulls up to 1024 bytes per tick from the PSRAM ringbuffer.
5. **Hardware (`i2s_audio_speaker`):**
   - Direct Memory Access (DMA) feeds the exact 32-bit datastream to the external DAC which actuates the amplifier.

### ⬆️ Sending Audio (Upstream)
1. **Hardware Microphones:** 2x INMP441 capture `32-bit Stereo` (Voice = Left, Echo Reference = Right).
2. **C++ `GeminiWebSocketClient` (`gemini_websocket.h`):** Read the 32-bit stream.
3. **FastAPI Python Bridge (`session.py`):**
   - Uses `struct.unpack_from` and slicing `data[0::8] + data[1::8]...` to mathematically demux the 32-bit Stereo array and isolate **only** the Left voice channel, discarding the acoustic echo signature.
   - Downsamples via `audioop` to Gemini's expected 16kHz `16-bit` Mono.
4. **Gemini Live API:** Receives crisp, echo-free voice.
