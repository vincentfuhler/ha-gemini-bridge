#include "esphome.h"
#include "esp_websocket_client.h"
#include "esp_heap_caps.h"
#include <mutex>
#include <vector>
#include <functional>

using namespace esphome;

// Always-on Gemini WebSocket bridge.
// The ESP32 streams mic audio continuously whenever the WebSocket is connected.
// The Python bridge's /api/activate endpoint controls whether it forwards to Gemini.
class GeminiWebSocketClient : public Component {
 protected:
  microphone::Microphone *mic_;
  speaker::Speaker *speaker_;

  esp_websocket_client_handle_t client_ = nullptr;
  std::string url_;

  uint8_t* audio_buffer_ = nullptr;
  size_t read_idx_ = 0;
  size_t write_idx_ = 0;
  size_t avail_len_ = 0;
  const size_t BUFFER_SIZE = 192000;

  uint32_t total_bytes_received_ = 0;
  uint32_t total_bytes_played_ = 0;
  uint32_t chunk_counter_ = 0;
  uint32_t play_zero_counter_ = 0;
  std::mutex audio_mutex_;

  bool first_audio_received_ = false;
  bool first_audio_played_ = false;
  bool speaker_started_ = false;

  TaskHandle_t playback_task_handle_ = nullptr;
  uint32_t last_connected_time_ = 0;
  bool is_connected_ = false;
  uint32_t last_disconnect_time_ = 0;
  std::function<void(std::string)> on_state_callback_;
  std::mutex state_mutex_;
  std::string pending_state_ = "";
  bool has_pending_state_ = false;

  void trigger_state(const std::string& state) {
      std::lock_guard<std::mutex> lock(state_mutex_);
      pending_state_ = state;
      has_pending_state_ = true;
  }

 public:
  GeminiWebSocketClient(const std::string& url, microphone::Microphone *mic = nullptr,
                        speaker::Speaker *speaker = nullptr)
      : url_(url), mic_(mic), speaker_(speaker) {}
      
  void set_state_callback(std::function<void(std::string)> cb) { on_state_callback_ = cb; }

  ~GeminiWebSocketClient() {
      if (playback_task_handle_ != nullptr) vTaskDelete(playback_task_handle_);
      if (audio_buffer_ != nullptr) heap_caps_free(audio_buffer_);
  }

  // ─── FREERTOS PLAYBACK TASK ───────────────────────────────────────────────

  static void playback_task(void *pvParameters) {
    auto *self = static_cast<GeminiWebSocketClient*>(pvParameters);
    ESP_LOGI("gemini_ws", "[PlaybackTask] Started on Core %d", xPortGetCoreID());

    while (true) {
        if (self->avail_len_ == 0 || self->speaker_ == nullptr) {
            vTaskDelay(pdMS_TO_TICKS(5));
            continue;
        }

        // Start speaker with correct stream info on first audio
        if (!self->speaker_started_) {
            ESP_LOGI("gemini_ws", "[PlaybackTask] Starting media_mixing_input (16-bit 48kHz stereo)...");
            audio::AudioStreamInfo stream_info(16, 2, 48000);
            self->speaker_->set_audio_stream_info(stream_info);
            self->speaker_->start();
            vTaskDelay(pdMS_TO_TICKS(100));
            self->speaker_started_ = true;
            ESP_LOGI("gemini_ws", "[PlaybackTask] Speaker is_running=%s",
                     self->speaker_->is_running() ? "YES" : "NO");
        }

        if (!self->speaker_->is_running()) {
            self->speaker_->start();
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }

        std::unique_lock<std::mutex> lock(self->audio_mutex_);
        if (self->avail_len_ == 0) continue;

        // Pre-buffer ~85ms of audio before starting playback to prevent stuttering
        if (!self->first_audio_played_ && self->avail_len_ < 16384) {
            continue;
        }

        size_t contiguous = self->BUFFER_SIZE - self->read_idx_;
        if (contiguous > self->avail_len_) contiguous = self->avail_len_;
        if (contiguous > 2048) contiguous = 2048;

        uint8_t* play_ptr = self->audio_buffer_ + self->read_idx_;
        lock.unlock(); // ENTSPERREN: Netzwerk-Thread kann jetzt wieder arbeiten!

        // BLOCKIERENDER HARDWARE-CALL (Netzwerk traffic läuft im Hintergrund munter weiter!)
        size_t written = self->speaker_->play(play_ptr, contiguous);

        lock.lock(); // SPERREN: Variablen sauber updaten
        self->total_bytes_played_ += written;

        if (written == 0) {
            self->play_zero_counter_++;
            lock.unlock();
            vTaskDelay(pdMS_TO_TICKS(1));
            
            // Hardware Freeze Recovery built-in here:
            if (self->play_zero_counter_ > 500) {
                 ESP_LOGW("gemini_ws", "Speaker I2S blocked for 500ms, restarting DMA engine...");
                 self->speaker_->stop();
                 vTaskDelay(pdMS_TO_TICKS(10));
                 self->speaker_->start();
                 self->play_zero_counter_ = 0;
            }
            continue;
        } else {
            self->play_zero_counter_ = 0;
            if (!self->first_audio_played_) {
                self->first_audio_played_ = true;
                ESP_LOGI("gemini_ws", "[PlaybackTask] First bytes played by hardware!");
            }
            self->read_idx_ = (self->read_idx_ + written) % self->BUFFER_SIZE;
            self->avail_len_ -= written;
        }
    }
  }

  // ─── SETUP ────────────────────────────────────────────────────────────────

  void setup() override {
    ESP_LOGI("gemini_ws", "Initializing Gemini WebSocket Client to %s", url_.c_str());

    audio_buffer_ = (uint8_t*)heap_caps_malloc(BUFFER_SIZE, MALLOC_CAP_SPIRAM);
    if (audio_buffer_ == nullptr) {
        ESP_LOGE("gemini_ws", "FATAL: PSRAM buffer allocation failed!");
        return;
    }
    ESP_LOGI("gemini_ws", "PSRAM audio buffer: %u bytes", BUFFER_SIZE);

    xTaskCreatePinnedToCore(playback_task, "gemini_playback", 8192, this, 5,
                            &playback_task_handle_, 0);

    esp_websocket_client_config_t cfg = {};
    cfg.uri = url_.c_str();
    cfg.reconnect_timeout_ms = 1000; // Increased reconnect speed
    cfg.network_timeout_ms = 60000;
    cfg.pingpong_timeout_sec = 120;
    // Set keep-alive to true just in case the firewall kills it
    cfg.disable_auto_reconnect = false;
    client_ = esp_websocket_client_init(&cfg);
    esp_websocket_register_events(client_, WEBSOCKET_EVENT_ANY,
                                  &GeminiWebSocketClient::websocket_event_handler, this);
    esp_websocket_client_start(client_);

    // Mic streams always — bridge decides whether to forward to Gemini
    if (mic_ != nullptr) {
        mic_->add_data_callback([this](const std::vector<uint8_t> &data) {
            if (client_ != nullptr && esp_websocket_client_is_connected(client_)) {
                // BUGFIX: Use a maximum upload timeout of 50ms instead of unendless portMAX_DELAY.
                // If it blocks from bad WiFi, drop this chunk so we don't crash the I2S/Watchdog.
                esp_websocket_client_send_bin(client_, (const char*)data.data(), data.size(), pdMS_TO_TICKS(50));
            }
        });
        mic_->start();
        ESP_LOGI("gemini_ws", "Microphone started. Streaming audio to bridge.");
    }
    last_connected_time_ = millis();
  }

  void loop() override {
      {
          std::lock_guard<std::mutex> lock(state_mutex_);
          if (has_pending_state_) {
              if (on_state_callback_) on_state_callback_(pending_state_);
              has_pending_state_ = false;
          }
      }

      // esp_websocket_client has its own auto-reconnect logic,
      // but if the Python bridge restarts abruptly, the TCP socket hangs permanently.
      // This non-blocking watchdog forces a hard reset of the socket if disconnected for > 5s.
      if (!is_connected_ && last_disconnect_time_ > 0) {
          if (millis() - last_disconnect_time_ > 5000) {
              ESP_LOGW("gemini_ws", "[Watchdog] 5s timeout reached. Forcing hard websocket client restart...");
              if (client_ != nullptr) {
                  esp_websocket_client_stop(client_);
                  // Small breather for the TCP stack to process closure
                  vTaskDelay(pdMS_TO_TICKS(150));
                  esp_websocket_client_start(client_);
              }
              last_disconnect_time_ = millis(); // Reset timer to try again in 5s
          }
      }
  }

  // ─── WEBSOCKET EVENT HANDLER ──────────────────────────────────────────────

  static void websocket_event_handler(void *handler_args, esp_event_base_t base,
                                       int32_t event_id, void *event_data) {
    auto *self = static_cast<GeminiWebSocketClient*>(handler_args);
    esp_websocket_event_data_t *data = (esp_websocket_event_data_t *)event_data;

    switch (event_id) {
        case WEBSOCKET_EVENT_CONNECTED:
            self->is_connected_ = true;
            self->last_disconnect_time_ = 0;
            ESP_LOGI("gemini_ws", "[WS] Connected to Gemini Bridge! Streaming mic audio.");
            self->trigger_state("connected");
            break;

        case WEBSOCKET_EVENT_DISCONNECTED:
            self->is_connected_ = false;
            self->last_disconnect_time_ = millis();
            ESP_LOGW("gemini_ws", "[WS] Disconnected. Fast reconnect executing...");
            self->trigger_state("disconnected");
            self->speaker_started_ = false;
            {
                std::lock_guard<std::mutex> lock(self->audio_mutex_);
                self->avail_len_ = 0;
                self->read_idx_ = 0;
                self->write_idx_ = 0;
                self->first_audio_played_ = false;
            }
            break;

        case WEBSOCKET_EVENT_DATA:
            if (data->op_code == 1 && data->data_len > 0) { // Text frame
                std::string payload((char*)data->data_ptr, data->data_len);
                ESP_LOGI("gemini_ws", "[WS] Text payload received: %s", payload.c_str());
                if (payload.find("\"state\": \"listening\"") != std::string::npos) {
                    self->trigger_state("listening");
                }
                else if (payload.find("\"state\": \"connected\"") != std::string::npos) {
                    self->trigger_state("connected");
                }
                else if (payload.find("\"state\": \"idle\"") != std::string::npos) {
                    self->trigger_state("idle");
                }
            }
            else if (data->op_code == 2 && self->speaker_ != nullptr) {
                std::lock_guard<std::mutex> lock(self->audio_mutex_);
                size_t to_write = data->data_len;
                if (self->avail_len_ + to_write <= self->BUFFER_SIZE) {
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
                        ESP_LOGI("gemini_ws", "FIRST AUDIO CHUNK from Gemini! (%u bytes)", to_write);
                    } else if (self->chunk_counter_ % 100 == 0) {
                        ESP_LOGI("gemini_ws", "Audio: Rcvd=%u Played=%u Avail=%u (Chunk #%u)",
                                 self->total_bytes_received_, self->total_bytes_played_,
                                 self->avail_len_, self->chunk_counter_);
                    }
                } else {
                    ESP_LOGW("gemini_ws", "Buffer overflow! Dropped %u bytes. (Avail: %u, Size: %u)", 
                             to_write, self->avail_len_, self->BUFFER_SIZE);
                }
            }
            break;
    }
  }
};
