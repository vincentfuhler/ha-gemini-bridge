#include "esphome.h"
#include "esp_websocket_client.h"
#include "esp_heap_caps.h"
#include <mutex>
#include <vector>

using namespace esphome;

// Custom ESPHome component that acts as a WebSocket bridge to Gemini.
//
// CRITICAL LESSONS LEARNED:
// - NEVER call i2s_audio_speaker->start() from this component!
//   The official Voice PE firmware manages the I2S hardware state machine.
//   Any external start() call creates a race condition that crashes the DMA
//   controller after ~25ms (observed empirically, v1.0.32).
// - Only manage media_mixing_input (the SourceSpeaker slot).
//   The Mixer will cascade correctly down to the hardware on its own.
// - Use a FreeRTOS task for playback (ESPHome loop() is not reliably called
//   for components registered via runtime lambda / App.register_component_).
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

  TaskHandle_t playback_task_handle_ = nullptr;

 public:
  std::function<void()> on_connected_ = nullptr;
  std::function<void()> on_disconnected_ = nullptr;

  GeminiWebSocketClient(const std::string& url, microphone::Microphone *mic = nullptr,
                        speaker::Speaker *speaker = nullptr)
      : url_(url), mic_(mic), speaker_(speaker) {}

  ~GeminiWebSocketClient() {
      if (playback_task_handle_ != nullptr) vTaskDelete(playback_task_handle_);
      if (audio_buffer_ != nullptr) heap_caps_free(audio_buffer_);
  }

  static void playback_task(void *pvParameters) {
    auto *self = static_cast<GeminiWebSocketClient*>(pvParameters);
    ESP_LOGI("gemini_ws", "[PlaybackTask] Playback FreeRTOS task started on Core %d!", xPortGetCoreID());

    while (true) {
        if (self->avail_len_ == 0 || self->speaker_ == nullptr) {
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }

        // Start media_mixing_input if not already done.
        // CRITICAL: set_audio_stream_info MUST be called BEFORE start()!
        // Without it, the Mixer has no format info and configures i2s_audio_speaker
        // with garbage settings, causing the "Audio stream settings not compatible" crash.
        // We set 16-bit here — the Mixer internally upscales 16->32 bit for the hardware.
        // DO NOT set 32-bit here — Mixer's source speakers only support 16-bit!
        if (!self->speaker_started_) {
            ESP_LOGI("gemini_ws", "[PlaybackTask] Setting stream info + starting media_mixing_input...");
            audio::AudioStreamInfo stream_info(16, 2, 48000);
            self->speaker_->set_audio_stream_info(stream_info);
            self->speaker_->start();
            vTaskDelay(pdMS_TO_TICKS(100));
            self->speaker_started_ = true;
            ESP_LOGI("gemini_ws", "[PlaybackTask] media_mixing_input started. is_running=%s",
                     self->speaker_->is_running() ? "YES" : "NO");
        }

        if (!self->speaker_->is_running()) {
            ESP_LOGW("gemini_ws", "[PlaybackTask] media_mixing_input stopped. Restarting...");
            self->speaker_->start();
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }

        std::lock_guard<std::mutex> lock(self->audio_mutex_);
        if (self->avail_len_ == 0) continue;

        size_t contiguous_avail = self->BUFFER_SIZE - self->read_idx_;
        if (contiguous_avail > self->avail_len_) contiguous_avail = self->avail_len_;
        if (contiguous_avail > 2048) contiguous_avail = 2048; // Limit to mixer ring buffer capacity

        const uint8_t *data_ptr = self->audio_buffer_ + self->read_idx_;
        size_t written = self->speaker_->play(data_ptr, contiguous_avail);
        self->total_bytes_played_ += written;

        if (written == 0) {
            self->play_zero_counter_++;
            if (self->play_zero_counter_ % 100 == 1) {
                ESP_LOGW("gemini_ws", "[PlaybackTask] play()=0 count=%u is_running=%s avail=%u",
                         self->play_zero_counter_,
                         self->speaker_->is_running() ? "yes" : "no",
                         self->avail_len_);
            }
            vTaskDelay(pdMS_TO_TICKS(5));
        } else {
            self->play_zero_counter_ = 0;
            if (!self->first_audio_played_) {
                self->first_audio_played_ = true;
                ESP_LOGI("gemini_ws", "🔊 SUCCESS: media_mixing_input consumed first %u bytes! Playback starting!", written);
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

    xTaskCreatePinnedToCore(
        playback_task, "gemini_playback",
        8192, this, 5, &playback_task_handle_, 0  // Core 0
    );

    esp_websocket_client_config_t websocket_cfg = {};
    websocket_cfg.uri = url_.c_str();
    client_ = esp_websocket_client_init(&websocket_cfg);
    esp_websocket_register_events(client_, WEBSOCKET_EVENT_ANY,
        &GeminiWebSocketClient::websocket_event_handler, this);
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
    static uint32_t loop_count = 0;
    loop_count++;
    if (loop_count == 1 || loop_count % 500 == 0) {
        ESP_LOGD("gemini_ws", "[loop()] Called %u times. avail=%u played=%u",
                 loop_count, avail_len_, total_bytes_played_);
    }
  }

  static void websocket_event_handler(void *handler_args, esp_event_base_t base,
                                       int32_t event_id, void *event_data) {
    auto *self = static_cast<GeminiWebSocketClient*>(handler_args);
    esp_websocket_event_data_t *data = (esp_websocket_event_data_t *)event_data;

    switch (event_id) {
        case WEBSOCKET_EVENT_CONNECTED:
            ESP_LOGI("gemini_ws", "[WS Thread] Connected to Gemini Bridge!");
            self->first_audio_received_ = false;
            self->first_audio_played_ = false;
            self->speaker_started_ = false;
            {
                std::lock_guard<std::mutex> lock(self->audio_mutex_);
                self->read_idx_ = 0; self->write_idx_ = 0; self->avail_len_ = 0;
            }
            self->total_bytes_received_ = 0;
            self->total_bytes_played_ = 0;
            self->chunk_counter_ = 0;
            self->play_zero_counter_ = 0;
            if (self->on_connected_) self->on_connected_();
            if (self->mic_ != nullptr) {
                ESP_LOGI("gemini_ws", "[WS Thread] Starting Microphone...");
                self->mic_->start();
            }
            break;

        case WEBSOCKET_EVENT_DISCONNECTED:
            ESP_LOGW("gemini_ws", "Disconnected");
            if (self->mic_ != nullptr) self->mic_->stop();
            if (self->speaker_ != nullptr) self->speaker_->stop();
            self->speaker_started_ = false;
            {
                std::lock_guard<std::mutex> lock(self->audio_mutex_);
                self->read_idx_ = 0; self->write_idx_ = 0; self->avail_len_ = 0;
            }
            break;

        case WEBSOCKET_EVENT_DATA:
            if (data->op_code == 2 && self->speaker_ != nullptr) {
                std::lock_guard<std::mutex> lock(self->audio_mutex_);
                size_t to_write = data->data_len;

                if (self->avail_len_ + to_write > self->BUFFER_SIZE) {
                    // Drop silently — overflow spam is not helpful
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
                        ESP_LOGI("gemini_ws", "🔊 FIRST AUDIO CHUNK RECEIVED! (%u bytes)", to_write);
                    } else if (self->chunk_counter_ % 50 == 0) {
                        ESP_LOGI("gemini_ws", "Audio Progress: Rcvd=%u Played=%u Avail=%u (Chunk #%u)",
                                 self->total_bytes_received_, self->total_bytes_played_,
                                 self->avail_len_, self->chunk_counter_);
                    }
                }
            }
            break;
    }
  }
};
