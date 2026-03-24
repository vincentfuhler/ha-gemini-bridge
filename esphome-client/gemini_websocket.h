#include "esphome.h"
#include "esp_websocket_client.h"
#include <queue>
#include <mutex>
#include <vector>

using namespace esphome;

// Custom ESPHome component that acts as a WebSocket bridge to Gemini
class GeminiWebSocketClient : public Component {
 protected:
  esp_websocket_client_handle_t client_ = nullptr;
  microphone::Microphone *mic_;
  speaker::Speaker *speaker_;
  std::string url_;

  std::queue<std::vector<uint8_t>> audio_queue_;
  std::mutex audio_mutex_;
  size_t current_chunk_offset_ = 0;

 public:
  GeminiWebSocketClient(std::string url, microphone::Microphone *mic, speaker::Speaker *speaker)
      : url_(url), mic_(mic), speaker_(speaker) {}

  void setup() override {
    ESP_LOGI("gemini_ws", "Initializing Gemini WebSocket Client to %s", this->url_.c_str());

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
    if (!this->audio_queue_.empty()) {
        auto &chunk = this->audio_queue_.front();
        size_t available = chunk.size() - this->current_chunk_offset_;
        const uint8_t *data_ptr = chunk.data() + this->current_chunk_offset_;
        
        size_t written = this->speaker_->play(data_ptr, available);
        this->current_chunk_offset_ += written;

        if (this->current_chunk_offset_ >= chunk.size()) {
            this->audio_queue_.pop();
            this->current_chunk_offset_ = 0;
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
            if (self->speaker_ != nullptr) {
                // Bridge sends 16-bit 16kHz mono PCM to the speaker
                audio::AudioStreamInfo info(16, 1, 16000);
                self->speaker_->set_audio_stream_info(info);
                self->speaker_->start();
            }
            break;
        case WEBSOCKET_EVENT_DISCONNECTED:
            ESP_LOGW("gemini_ws", "WebSocket Disconnected");
            if (self->speaker_ != nullptr) {
                self->speaker_->stop();
                std::lock_guard<std::mutex> lock(self->audio_mutex_);
                while(!self->audio_queue_.empty()) self->audio_queue_.pop();
                self->current_chunk_offset_ = 0;
            }
            break;
        case WEBSOCKET_EVENT_DATA:
            // Opcode 2 means Binary Data (Audio)
            if (data->op_code == 2 && self->speaker_ != nullptr) {
                std::lock_guard<std::mutex> lock(self->audio_mutex_);
                if (self->audio_queue_.size() < 400) {
                    std::vector<uint8_t> chunk((uint8_t*)data->data_ptr, (uint8_t*)data->data_ptr + data->data_len);
                    self->audio_queue_.push(std::move(chunk));
                } else {
                    ESP_LOGW("gemini_ws", "Audio queue full, dropping chunk!");
                }
            }
            break;
    }
  }
};
