#include "esphome.h"
#include "esp_websocket_client.h"

using namespace esphome;

// Custom ESPHome component that acts as a WebSocket bridge to Gemini
class GeminiWebSocketClient : public Component {
 protected:
  esp_websocket_client_handle_t client_ = nullptr;
  microphone::Microphone *mic_;
  speaker::Speaker *speaker_;
  std::string url_;

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

  // Handle incoming WebSocket data (e.g. audio from Gemini)
  static void websocket_event_handler(void *handler_args, esp_event_base_t base, int32_t event_id, void *event_data) {
    auto *self = static_cast<GeminiWebSocketClient*>(handler_args);
    esp_websocket_event_data_t *data = (esp_websocket_event_data_t *)event_data;
    
    switch (event_id) {
        case WEBSOCKET_EVENT_CONNECTED:
            ESP_LOGI("gemini_ws", "WebSocket Connected to Gemini Bridge!");
            if (self->speaker_ != nullptr) {
                // Bridge sends 32-bit 48kHz mono PCM to the speaker
                audio::AudioStreamInfo info(32, 1, 48000);
                self->speaker_->set_audio_stream_info(info);
                self->speaker_->start();
            }
            break;
        case WEBSOCKET_EVENT_DISCONNECTED:
            ESP_LOGW("gemini_ws", "WebSocket Disconnected");
            if (self->speaker_ != nullptr) {
                self->speaker_->stop();
            }
            break;
        case WEBSOCKET_EVENT_DATA:
            // Opcode 2 means Binary Data (Audio)
            if (data->op_code == 2 && self->speaker_ != nullptr) {
                // Play received PCM chunks (16-bit, 16kHz) directly to I2S speaker
                // ESP-IDF speakers usually accept raw uint8_t pointers
                self->speaker_->play((const uint8_t*)data->data_ptr, data->data_len);
            }
            break;
    }
  }
};
