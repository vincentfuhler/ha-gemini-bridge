#include "esphome.h"
#include "esp_websocket_client.h"
#include "esp_heap_caps.h"
#include <queue>
#include <mutex>
#include <vector>

using namespace esphome;

// Custom ESPHome component that acts as a WebSocket bridge to Gemini
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
  const size_t BUFFER_SIZE = 96000; // Large buffer for stability
  
  uint32_t total_bytes_received_ = 0;
  uint32_t total_bytes_played_ = 0;
  uint32_t chunk_counter_ = 0;
  std::mutex audio_mutex_;
  
  bool first_audio_received_ = false;
  bool first_audio_played_ = false;

 public:
  std::function<void()> on_connected_ = nullptr;
  std::function<void()> on_disconnected_ = nullptr;

  GeminiWebSocketClient(const std::string& url, microphone::Microphone *mic = nullptr, speaker::Speaker *speaker = nullptr, speaker::Speaker *i2s = nullptr) 
      : url_(url), mic_(mic), speaker_(speaker), i2s_(i2s) {}

  ~GeminiWebSocketClient() {
      if (this->audio_buffer_ != nullptr) {
          heap_caps_free(this->audio_buffer_);
      }
  }

  void setup() override {
    ESP_LOGI("gemini_ws", "Initializing Gemini WebSocket Client to %s", this->url_.c_str());

    // Explicitly allocate ringbuffer in PSRAM to prevent std::bad_alloc aborts
    this->audio_buffer_ = (uint8_t*)heap_caps_malloc(this->BUFFER_SIZE, MALLOC_CAP_SPIRAM);
    if (this->audio_buffer_ == nullptr) {
        ESP_LOGE("gemini_ws", "Failed to allocate audio buffer in PSRAM!");
        return; // Don't proceed if allocation failed
    }

    // Configure WebSocket Client (requires esp-idf framework in ESPHome)
    esp_websocket_client_config_t websocket_cfg = {};
    websocket_cfg.uri = this->url_.c_str();

    this->client_ = esp_websocket_client_init(&websocket_cfg);
    esp_websocket_register_events(this->client_, WEBSOCKET_EVENT_ANY, &GeminiWebSocketClient::websocket_event_handler, this);
    esp_websocket_client_start(this->client_);

    // Register microphone callback to stream audio to WebSocket
    if (this->mic_ != nullptr) {
      this->mic_->add_data_callback([this](const std::vector<uint8_t> &data) {
        if (this->client_ != nullptr && esp_websocket_client_is_connected(this->client_)) {
          // Send raw PCM binary data chunks
          esp_websocket_client_send_bin(this->client_, (const char*)data.data(), data.size(), portMAX_DELAY);
        }
      });
      // Start microphone recording 
      this->mic_->start();
    }
  }

  void loop() override {
    if (this->speaker_ == nullptr) return;

    std::lock_guard<std::mutex> lock(this->audio_mutex_);
    if (this->avail_len_ > 0) {
        // Write the largest contiguous chunk possible
        size_t contiguous_avail = this->BUFFER_SIZE - this->read_idx_;
        if (contiguous_avail > this->avail_len_) {
            contiguous_avail = this->avail_len_;
        }
        
        const uint8_t *data_ptr = this->audio_buffer_ + this->read_idx_;
        
        // Ensure the entire Speaker pipeline hasn't aborted due to buffer starvation
        if (!this->speaker_->is_running() || (this->i2s_ && !this->i2s_->is_running())) {
            ESP_LOGW("gemini_ws", "Hardware Speaker pipeline stopped! Forcefully waking root I2S DAC...");
            if (this->i2s_) this->i2s_->start();
            this->speaker_->start();
        }
        
        size_t written = this->speaker_->play(data_ptr, contiguous_avail);
        this->total_bytes_played_ += written;
        
        if (written < contiguous_avail) {
            ESP_LOGW("gemini_ws", "Mixer Buffer Pressure: Provided %d bytes, speaker accepted %d bytes.", contiguous_avail, written);
        }
        
        if (written > 0) {
            if (!this->first_audio_played_) {
                this->first_audio_played_ = true;
                ESP_LOGI("gemini_ws", "🔊 SUCCESS: Hardware Speaker consumed its first bytes! Playback is physically starting!");
            }
            this->read_idx_ = (this->read_idx_ + written) % this->BUFFER_SIZE;
            this->avail_len_ -= written;
            ESP_LOGV("gemini_ws", "Speaker consumed %d bytes (Remaining: %d)", written, this->avail_len_);
        }
    }
  }

  // Handle incoming WebSocket data (e.g. audio from Gemini)
  static void websocket_event_handler(void *handler_args, esp_event_base_t base, int32_t event_id, void *event_data) {
    auto *self = static_cast<GeminiWebSocketClient*>(handler_args);
    esp_websocket_event_data_t *data = (esp_websocket_event_data_t *)event_data;
    
    switch (event_id) {
        case WEBSOCKET_EVENT_CONNECTED:
            ESP_LOGI("gemini_ws", "WebSocket Connected to Gemini Bridge!");
            self->first_audio_received_ = false;
            self->first_audio_played_ = false;
            // Clear buffer on new connection
            if (self->audio_buffer_ != nullptr) {
                self->read_idx_ = 0;
                self->write_idx_ = 0;
                self->avail_len_ = 0;
            }
            self->total_bytes_received_ = 0;
            self->total_bytes_played_ = 0;
            self->chunk_counter_ = 0;
            
            if (self->on_connected_) self->on_connected_();
            
            if (self->mic_ != nullptr) {
                ESP_LOGI("gemini_ws", "Starting ESP32 Microphone...");
                self->mic_->start();
            }
            if (self->speaker_ != nullptr) {
                // Bridge sends 32-bit 48kHz Stereo PCM to directly match Voice PE DAC hardware constraints!
                audio::AudioStreamInfo info(32, 2, 48000);
                self->speaker_->set_audio_stream_info(info);
                if (self->i2s_) self->i2s_->start();
                self->speaker_->start();
            }
            break;
        case WEBSOCKET_EVENT_DISCONNECTED:
            ESP_LOGW("gemini_ws", "WebSocket Disconnected");
            if (self->mic_ != nullptr) {
                self->mic_->stop();
            }
            if (self->speaker_ != nullptr) {
                self->speaker_->stop();
                std::lock_guard<std::mutex> lock(self->audio_mutex_);
                self->read_idx_ = 0;
                self->write_idx_ = 0;
                self->avail_len_ = 0;
            }
            break;
        case WEBSOCKET_EVENT_DATA:
            // Opcode 2 means Binary Data (Audio)
            if (data->op_code == 2 && self->speaker_ != nullptr) {
                std::lock_guard<std::mutex> lock(self->audio_mutex_);
                size_t to_write = data->data_len;
                // Mute this ultra-verbose chunk log, we will summarize progress instead!
                // ESP_LOGD("gemini_ws", "Received Binary Audio Chunk: %d bytes", to_write);
                
                if (self->avail_len_ + to_write > self->BUFFER_SIZE) {
                    ESP_LOGW("gemini_ws", "Audio buffer OVERFLOW! Dropping incoming audio chunks from Bridge.");
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
                        ESP_LOGI("gemini_ws", "🔊 SUCCESS: ESP32 RECEIVED THE FIRST AUDIO RESPONSE CHUNK FROM BRIDGE! (%d bytes)", to_write);
                    } else if (self->chunk_counter_ % 50 == 0) {
                        ESP_LOGI("gemini_ws", "Audio Stream Progress: Received %u bytes, Played %u bytes (Chunk #%u)", 
                                 self->total_bytes_received_, self->total_bytes_played_, self->chunk_counter_);
                    }
                }
            }
            break;
    }
  }
};
