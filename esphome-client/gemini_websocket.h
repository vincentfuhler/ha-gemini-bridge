#include "esphome.h"
#include "esp_websocket_client.h"
#include "esp_heap_caps.h"
#include <mutex>
#include <vector>

using namespace esphome;

// Custom ESPHome component that acts as a WebSocket bridge to Gemini.
//
// SESSION LIFECYCLE:
//   1. Wake word "Okay Nabu" detected → voice_pe.yaml calls startSession()
//   2. Microphone starts streaming → Gemini responds → audio plays
//   3. 20 seconds after last Gemini audio → stopSession() is called automatically
//   4. Device returns to idle wake-word listening state
//
// THREAD SAFETY:
//   - WebSocket events run in IDF timer thread (Core 1)
//   - Audio playback runs in dedicated FreeRTOS task (Core 0)
//   - Speaker start/stop is only called from the FreeRTOS playback task
class GeminiWebSocketClient : public Component {
 protected:
  microphone::Microphone *mic_;
  speaker::Speaker *speaker_;  // Points to media_mixing_input ONLY

  esp_websocket_client_handle_t client_ = nullptr;
  std::string url_;

  uint8_t* audio_buffer_ = nullptr;
  size_t read_idx_ = 0;
  size_t write_idx_ = 0;
  size_t avail_len_ = 0;
  const size_t BUFFER_SIZE = 96000;

  uint32_t total_bytes_received_ = 0;
  uint32_t total_bytes_played_ = 0;
  uint32_t chunk_counter_ = 0;
  uint32_t play_zero_counter_ = 0;
  std::mutex audio_mutex_;

  bool first_audio_received_ = false;
  bool first_audio_played_ = false;
  bool speaker_started_ = false;
  bool session_active_ = false;

  // Auto-stop: timestamp of last Gemini audio chunk. Session ends 20s after this.
  uint32_t last_audio_ms_ = 0;
  const uint32_t SESSION_TIMEOUT_MS = 20000;

  TaskHandle_t playback_task_handle_ = nullptr;
  TaskHandle_t watchdog_task_handle_ = nullptr;

 public:
  std::function<void()> on_connected_ = nullptr;
  std::function<void()> on_disconnected_ = nullptr;
  std::function<void()> on_session_ended_ = nullptr;  // Called when session auto-expires

  GeminiWebSocketClient(const std::string& url, microphone::Microphone *mic = nullptr,
                        speaker::Speaker *speaker = nullptr)
      : url_(url), mic_(mic), speaker_(speaker) {}

  ~GeminiWebSocketClient() {
      if (playback_task_handle_ != nullptr) vTaskDelete(playback_task_handle_);
      if (watchdog_task_handle_ != nullptr) vTaskDelete(watchdog_task_handle_);
      if (audio_buffer_ != nullptr) heap_caps_free(audio_buffer_);
  }

  // ─── PUBLIC API ────────────────────────────────────────────────────────────

  bool isSessionActive() const { return session_active_; }

  // Called from voice_pe.yaml on_wake_word_detected or button press
  void startSession() {
      if (session_active_) {
          ESP_LOGW("gemini_ws", "startSession() called but session already active. Ignoring.");
          return;
      }
      ESP_LOGI("gemini_ws", "🎙️ Wake word detected! Starting Gemini session...");
      session_active_ = true;
      last_audio_ms_ = millis();  // Start timeout clock now

      // Reset buffer
      {
          std::lock_guard<std::mutex> lock(audio_mutex_);
          read_idx_ = 0; write_idx_ = 0; avail_len_ = 0;
      }
      total_bytes_received_ = 0; total_bytes_played_ = 0;
      chunk_counter_ = 0; play_zero_counter_ = 0;
      first_audio_received_ = false; first_audio_played_ = false;
      speaker_started_ = false;

      // Note: We do NOT call mic_->start() here!
      // microWakeWord already started the mic for wake word detection.
      // Our add_data_callback() receives mic data and gates it on session_active_.
      ESP_LOGI("gemini_ws", "Session active. Mic audio will now be forwarded to Gemini.");
  }

  // Called manually or by the auto-timeout watchdog
  void stopSession() {
      if (!session_active_) return;
      ESP_LOGI("gemini_ws", "⏹ Stopping Gemini session.");
      session_active_ = false;

      // Note: We do NOT call mic_->stop() here!
      // microWakeWord must keep the mic running for the next wake word detection.
      // We only gate our data forwarding via session_active_.
      ESP_LOGI("gemini_ws", "Session inactive. Mic audio will be suppressed until next wake word.");
      if (speaker_ != nullptr) speaker_->stop();
      speaker_started_ = false;

      // Clear audio buffer
      {
          std::lock_guard<std::mutex> lock(audio_mutex_);
          read_idx_ = 0; write_idx_ = 0; avail_len_ = 0;
      }

      if (on_session_ended_) on_session_ended_();
  }

  // ─── FREERTOS TASKS ────────────────────────────────────────────────────────

  static void playback_task(void *pvParameters) {
    auto *self = static_cast<GeminiWebSocketClient*>(pvParameters);
    ESP_LOGI("gemini_ws", "[PlaybackTask] Started on Core %d", xPortGetCoreID());

    while (true) {
        if (!self->session_active_ || self->avail_len_ == 0 || self->speaker_ == nullptr) {
            vTaskDelay(pdMS_TO_TICKS(5));
            continue;
        }

        // Start media_mixing_input once per session with correct stream info
        if (!self->speaker_started_) {
            ESP_LOGI("gemini_ws", "[PlaybackTask] Starting media_mixing_input (16-bit 48kHz stereo)...");
            audio::AudioStreamInfo stream_info(16, 2, 48000);
            self->speaker_->set_audio_stream_info(stream_info);
            self->speaker_->start();
            vTaskDelay(pdMS_TO_TICKS(100));
            self->speaker_started_ = true;
            ESP_LOGI("gemini_ws", "[PlaybackTask] Speaker started: is_running=%s",
                     self->speaker_->is_running() ? "YES" : "NO");
        }

        if (!self->speaker_->is_running()) {
            ESP_LOGW("gemini_ws", "[PlaybackTask] Speaker stopped. Restarting...");
            self->speaker_->start();
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }

        std::lock_guard<std::mutex> lock(self->audio_mutex_);
        if (self->avail_len_ == 0) continue;

        size_t contiguous = self->BUFFER_SIZE - self->read_idx_;
        if (contiguous > self->avail_len_) contiguous = self->avail_len_;
        if (contiguous > 2048) contiguous = 2048;

        size_t written = self->speaker_->play(self->audio_buffer_ + self->read_idx_, contiguous);
        self->total_bytes_played_ += written;

        if (written == 0) {
            self->play_zero_counter_++;
            vTaskDelay(pdMS_TO_TICKS(1));
        } else {
            self->play_zero_counter_ = 0;
            if (!self->first_audio_played_) {
                self->first_audio_played_ = true;
                ESP_LOGI("gemini_ws", "🔊 First bytes played by hardware!");
            }
            self->read_idx_ = (self->read_idx_ + written) % self->BUFFER_SIZE;
            self->avail_len_ -= written;
        }
    }
  }

  // Watchdog: ends session 20s after last Gemini audio
  static void watchdog_task(void *pvParameters) {
    auto *self = static_cast<GeminiWebSocketClient*>(pvParameters);
    ESP_LOGI("gemini_ws", "[Watchdog] Session watchdog started.");

    while (true) {
        vTaskDelay(pdMS_TO_TICKS(1000));  // Check every second

        if (!self->session_active_) continue;

        uint32_t now = millis();
        uint32_t elapsed = now - self->last_audio_ms_;

        if (elapsed >= self->SESSION_TIMEOUT_MS) {
            ESP_LOGI("gemini_ws", "⏰ Session timeout! No Gemini audio for %u ms. Ending session.", elapsed);
            self->stopSession();
        } else if (elapsed > (self->SESSION_TIMEOUT_MS / 2)) {
            uint32_t remaining = (self->SESSION_TIMEOUT_MS - elapsed) / 1000;
            ESP_LOGD("gemini_ws", "[Watchdog] Session ends in %u seconds (no activity)", remaining);
        }
    }
  }

  // ─── SETUP/LOOP ────────────────────────────────────────────────────────────

  void setup() override {
    ESP_LOGI("gemini_ws", "Initializing Gemini WebSocket Client → %s", url_.c_str());

    audio_buffer_ = (uint8_t*)heap_caps_malloc(BUFFER_SIZE, MALLOC_CAP_SPIRAM);
    if (audio_buffer_ == nullptr) {
        ESP_LOGE("gemini_ws", "FATAL: PSRAM buffer allocation failed!");
        return;
    }
    ESP_LOGI("gemini_ws", "PSRAM buffer: %u bytes", BUFFER_SIZE);

    // Spawn playback task (Core 0 — away from WiFi)
    xTaskCreatePinnedToCore(playback_task, "gemini_playback", 8192, this, 5,
                            &playback_task_handle_, 0);
    // Spawn session watchdog task
    xTaskCreatePinnedToCore(watchdog_task, "gemini_watchdog", 4096, this, 3,
                            &watchdog_task_handle_, 0);

    // Connect WebSocket once at boot — stays connected for low-latency session starts
    esp_websocket_client_config_t cfg = {};
    cfg.uri = url_.c_str();
    cfg.reconnect_timeout_ms = 5000;
    client_ = esp_websocket_client_init(&cfg);
    esp_websocket_register_events(client_, WEBSOCKET_EVENT_ANY,
                                  &GeminiWebSocketClient::websocket_event_handler, this);
    esp_websocket_client_start(client_);

    // Register mic callback (mic only sends when session_active_)
    if (mic_ != nullptr) {
      mic_->add_data_callback([this](const std::vector<uint8_t> &data) {
        if (!session_active_) return;  // Silent when idle
        if (client_ != nullptr && esp_websocket_client_is_connected(client_)) {
          esp_websocket_client_send_bin(client_, (const char*)data.data(), data.size(), portMAX_DELAY);
        }
      });
      // Note: mic_->start() is called by startSession(), NOT here!
    }
  }

  void loop() override {
    // Kept for ESPHome compatibility. Real work is in FreeRTOS tasks.
    static uint32_t loop_count = 0;
    if (++loop_count == 1) {
        ESP_LOGI("gemini_ws", "[loop()] ESPHome Main Loop confirmed running.");
    }
  }

  // ─── WEBSOCKET EVENT HANDLER ───────────────────────────────────────────────

  static void websocket_event_handler(void *handler_args, esp_event_base_t base,
                                       int32_t event_id, void *event_data) {
    auto *self = static_cast<GeminiWebSocketClient*>(handler_args);
    esp_websocket_event_data_t *data = (esp_websocket_event_data_t *)event_data;

    switch (event_id) {
        case WEBSOCKET_EVENT_CONNECTED:
            ESP_LOGI("gemini_ws", "[WS] Connected to Gemini Bridge. Waiting for wake word...");
            if (self->on_connected_) self->on_connected_();
            break;

        case WEBSOCKET_EVENT_DISCONNECTED:
            ESP_LOGW("gemini_ws", "[WS] Disconnected. Will reconnect automatically.");
            if (self->session_active_) self->stopSession();
            break;

        case WEBSOCKET_EVENT_DATA:
            if (data->op_code == 2 && self->speaker_ != nullptr && self->session_active_) {
                std::lock_guard<std::mutex> lock(self->audio_mutex_);
                size_t to_write = data->data_len;

                // Update last-audio timestamp for session watchdog
                self->last_audio_ms_ = millis();

                if (self->avail_len_ + to_write > self->BUFFER_SIZE) {
                    // Silently drop — avoid spam
                } else {
                    const uint8_t* src = (const uint8_t*)data->data_ptr;
                    for (size_t i = 0; i < to_write; i++) {
                        self->audio_buffer_[self->write_idx_] = src[i];
                        self->write_idx_ = (self->write_idx_ + 1) % self->BUFFER_SIZE;
                    }
                    self->avail_len_ += to_write;
                    self->chunk_counter_++;
                    self->total_bytes_received_ += to_write;

                    if (!self->first_audio_received_) {
                        self->first_audio_received_ = true;
                        ESP_LOGI("gemini_ws", "🔊 FIRST AUDIO CHUNK from Gemini! (%u bytes)", to_write);
                    } else if (self->chunk_counter_ % 100 == 0) {
                        ESP_LOGI("gemini_ws", "Audio: Rcvd=%u Played=%u Avail=%u (Chunk #%u)",
                                 self->total_bytes_received_, self->total_bytes_played_,
                                 self->avail_len_, self->chunk_counter_);
                    }
                }
            }
            break;
    }
  }
};
