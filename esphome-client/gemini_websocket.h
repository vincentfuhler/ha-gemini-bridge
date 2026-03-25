#include "esphome.h"
#include "esp_websocket_client.h"
#include "esp_heap_caps.h"
#include <mutex>
#include <vector>

using namespace esphome;

// Custom ESPHome component that acts as a WebSocket bridge to Gemini.
// 
// IMPORTANT LESSONS LEARNED:
// 1. App.register_component_() registers the component, but loop() may NOT be called
//    reliably if the WiFi on_connect lambda fires after the Main Loop has already
//    finalized its component scheduling cycle.
//
// 2. ESPHome's MixerSpeaker::SourceSpeaker::play() returns 0 if the source speaker task
//    has not been started. The start() call MUST happen before audio can flow.
//
// 3. The MixerSpeaker internally IS running from boot (see speaker_mixer:454: Starting).
//    The problem is that the SourceSpeaker (media_mixing_input) slot is NOT started.
//
// STRATEGY in this version: Use a dedicated FreeRTOS task for audio playback.
// Audio data is written from the WebSocket event handler into a PSRAM ring buffer.
// A dedicated FreeRTOS task (not ESPHome loop()) reads from the ring buffer and calls
// speaker->play(). This fully bypasses the ESPHome scheduling problem.
class GeminiWebSocketClient : public Component {
 protected:
  microphone::Microphone *mic_;
  speaker::Speaker *speaker_;
  speaker::Speaker *i2s_;

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

  TaskHandle_t playback_task_handle_ = nullptr;

 public:
  GeminiWebSocketClient(const std::string& url, microphone::Microphone *mic = nullptr,
                        speaker::Speaker *speaker = nullptr, speaker::Speaker *i2s = nullptr) 
      : url_(url), mic_(mic), speaker_(speaker), i2s_(i2s) {}

  ~GeminiWebSocketClient() {
      if (playback_task_handle_ != nullptr) {
          vTaskDelete(playback_task_handle_);
      }
      if (audio_buffer_ != nullptr) {
          heap_caps_free(audio_buffer_);
      }
  }

  // Static FreeRTOS task entry point
  static void playback_task(void *pvParameters) {
    auto *self = static_cast<GeminiWebSocketClient*>(pvParameters);
    ESP_LOGI("gemini_ws", "[PlaybackTask] Playback task started!");
    
    while (true) {
        // Wait if no audio available
        if (self->avail_len_ == 0 || self->speaker_ == nullptr) {
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }

        // Ensure speaker is started
        if (!self->speaker_started_) {
            ESP_LOGI("gemini_ws", "[PlaybackTask] Starting media_mixing_input and i2s_audio_speaker...");
            if (self->i2s_) self->i2s_->start();
            self->speaker_->start();
            vTaskDelay(pdMS_TO_TICKS(50)); // Give time for start to complete
            self->speaker_started_ = true;
            ESP_LOGI("gemini_ws", "[PlaybackTask] speaker started. is_running=%s", 
                     self->speaker_->is_running() ? "YES" : "NO");
        }

        // Recover if speaker stopped
        if (!self->speaker_->is_running()) {
            ESP_LOGW("gemini_ws", "[PlaybackTask] Speaker stopped! Restarting...");
            if (self->i2s_) self->i2s_->start();
            self->speaker_->start();
            vTaskDelay(pdMS_TO_TICKS(50));
        }

        std::lock_guard<std::mutex> lock(self->audio_mutex_);
        if (self->avail_len_ == 0) continue;

        size_t contiguous_avail = self->BUFFER_SIZE - self->read_idx_;
        if (contiguous_avail > self->avail_len_) {
            contiguous_avail = self->avail_len_;
        }
        // Limit to reasonable chunk for play()
        if (contiguous_avail > 4096) contiguous_avail = 4096;

        const uint8_t *data_ptr = self->audio_buffer_ + self->read_idx_;
        size_t written = self->speaker_->play(data_ptr, contiguous_avail);
        self->total_bytes_played_ += written;

        if (written == 0) {
            self->play_zero_counter_++;
            if (self->play_zero_counter_ % 50 == 1) {
                ESP_LOGW("gemini_ws", "[PlaybackTask] play() returned 0! count=%u is_running=%s avail=%u",
                         self->play_zero_counter_,
                         self->speaker_->is_running() ? "yes" : "no",
                         self->avail_len_);
            }
            // Small backoff to let the mixer drain
            vTaskDelay(pdMS_TO_TICKS(5));
        } else {
            self->play_zero_counter_ = 0;
            if (!self->first_audio_played_) {
                self->first_audio_played_ = true;
                ESP_LOGI("gemini_ws", "🔊 SUCCESS: Hardware Speaker consumed its FIRST bytes! PLAYBACK IS STARTING!");
            }
            self->read_idx_ = (self->read_idx_ + written) % self->BUFFER_SIZE;
            self->avail_len_ -= written;
        }
    }
  }

  void setup() override {
    ESP_LOGI("gemini_ws", "Initializing Gemini WebSocket Client to %s", url_.c_str());

    audio_buffer_ = (uint8_t*)heap_caps_malloc(BUFFER_SIZE, MALLOC_CAP_SPIRAM);
    if (audio_buffer_ == nullptr) {
        ESP_LOGE("gemini_ws", "FATAL: Failed to allocate %u byte audio buffer in PSRAM!", BUFFER_SIZE);
        return;
    }
    ESP_LOGI("gemini_ws", "PSRAM audio buffer allocated: %u bytes", BUFFER_SIZE);

    // Spawn dedicated playback task pinned to Core 0 (opposite of WiFi/Network Core 1)
    // This ensures audio playback never competes with network I/O for CPU time
    xTaskCreatePinnedToCore(
        playback_task,
        "gemini_playback",
        8192,          // Stack size
        this,          // Parameter
        5,             // Priority (below WiFi but above idle)
        &playback_task_handle_,
        0              // Core 0 (ESPHome main loop runs on Core 1 by default)
    );
    ESP_LOGI("gemini_ws", "Playback FreeRTOS task spawned on Core 0");

    esp_websocket_client_config_t websocket_cfg = {};
    websocket_cfg.uri = url_.c_str();

    client_ = esp_websocket_client_init(&websocket_cfg);
    esp_websocket_register_events(client_, WEBSOCKET_EVENT_ANY, &GeminiWebSocketClient::websocket_event_handler, this);
    esp_websocket_client_start(client_);

    if (mic_ != nullptr) {
      mic_->add_data_callback([this](const std::vector<uint8_t> &data) {
        if (client_ != nullptr && esp_websocket_client_is_connected(client_)) {
          esp_websocket_client_send_bin(client_, (const char*)data.data(), data.size(), portMAX_DELAY);
        }
      });
      mic_->start();
    }
  }

  void loop() override {
    // Kept for ESPHome compatibility but audio playback is handled by the dedicated FreeRTOS task.
    // This will log once to confirm loop() IS being called.
    static uint32_t loop_count = 0;
    loop_count++;
    if (loop_count == 1 || loop_count % 1000 == 0) {
        ESP_LOGI("gemini_ws", "[loop()] Called %u times. avail=%u played=%u", 
                 loop_count, avail_len_, total_bytes_played_);
    }
  }

  static void websocket_event_handler(void *handler_args, esp_event_base_t base, int32_t event_id, void *event_data) {
    auto *self = static_cast<GeminiWebSocketClient*>(handler_args);
    esp_websocket_event_data_t *data = (esp_websocket_event_data_t *)event_data;
    
    switch (event_id) {
        case WEBSOCKET_EVENT_CONNECTED:
            ESP_LOGI("gemini_ws", "[WS Thread] WebSocket Connected to Gemini Bridge!");
            self->first_audio_received_ = false;
            self->first_audio_played_ = false;
            self->speaker_started_ = false; // Reset so playback task restarts speaker
            {
                std::lock_guard<std::mutex> lock(self->audio_mutex_);
                self->read_idx_ = 0;
                self->write_idx_ = 0;
                self->avail_len_ = 0;
            }
            self->total_bytes_received_ = 0;
            self->total_bytes_played_ = 0;
            self->chunk_counter_ = 0;
            self->play_zero_counter_ = 0;

            if (self->on_connected_) self->on_connected_();

            if (self->mic_ != nullptr) {
                ESP_LOGI("gemini_ws", "[WS Thread] Starting ESP32 Microphone...");
                self->mic_->start();
            }
            break;

        case WEBSOCKET_EVENT_DISCONNECTED:
            ESP_LOGW("gemini_ws", "WebSocket Disconnected");
            if (self->mic_ != nullptr) self->mic_->stop();
            if (self->speaker_ != nullptr) self->speaker_->stop();
            self->speaker_started_ = false;
            {
                std::lock_guard<std::mutex> lock(self->audio_mutex_);
                self->read_idx_ = 0;
                self->write_idx_ = 0;
                self->avail_len_ = 0;
            }
            break;

        case WEBSOCKET_EVENT_DATA:
            if (data->op_code == 2 && self->speaker_ != nullptr) {
                std::lock_guard<std::mutex> lock(self->audio_mutex_);
                size_t to_write = data->data_len;

                if (self->avail_len_ + to_write > self->BUFFER_SIZE) {
                    ESP_LOGW("gemini_ws", "Audio buffer OVERFLOW! Dropping %u bytes.", to_write);
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
                        ESP_LOGI("gemini_ws", "🔊 FIRST AUDIO CHUNK RECEIVED from Bridge! (%u bytes)", to_write);
                    } else if (self->chunk_counter_ % 50 == 0) {
                        ESP_LOGI("gemini_ws", "Audio Progress: Received %u bytes, Played %u bytes (Chunk #%u)",
                                 self->total_bytes_received_, self->total_bytes_played_, self->chunk_counter_);
                    }
                }
            }
            break;
    }
  }

  std::function<void()> on_connected_ = nullptr;
  std::function<void()> on_disconnected_ = nullptr;
};
