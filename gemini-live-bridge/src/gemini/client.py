import asyncio
import base64
import json
import os
import websockets
from typing import Callable, Any

from src.api.routes import set_bridge_active
from src.logging import setup_logger
from src.config import settings
from src.gemini.tools import HA_TOOLS, MEMORY_FILE
from src.ha import HomeAssistantClient

logger = setup_logger("gemini_client")


def _load_system_prompt() -> str | None:
    """
    Load system prompt from the configured file path.
    Falls back to the bundled default if the user file doesn't exist, and auto-creates it.
    """
    user_path = settings.SYSTEM_PROMPT_FILE
    bundled_path = "/app/system_prompt.txt"
    dev_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "system_prompt.txt")

    # If the user file doesn't exist, seed it from the original
    if not os.path.exists(user_path):
        source_path = bundled_path if os.path.exists(bundled_path) else dev_path
        if os.path.exists(source_path):
            try:
                os.makedirs(os.path.dirname(user_path), exist_ok=True)
                with open(source_path, "r", encoding="utf-8") as src, open(user_path, "w", encoding="utf-8") as dst:
                    dst.write(src.read())
                logger.info(f"✨ Auto-created default system prompt at {user_path}")
            except Exception as e:
                logger.warning(f"Could not auto-create {user_path}: {e}")

    paths = [user_path, bundled_path, dev_path]
    for path in paths:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    prompt = f.read().strip()
                    logger.info(f"System prompt loaded from {path} ({len(prompt)} chars)")
                    return prompt
            except Exception as e:
                logger.warning(f"Failed to read system prompt from {path}: {e}")
                
    logger.warning("No system prompt file found. Starting without system instruction.")
    return None


class GeminiLiveClient:
    """
    Client for the Gemini Live / Multimodal API.
    Supports:
    - System prompt (loaded from file)
    - Function calling for Home Assistant control
    - Real-time bidirectional audio
    """
    def __init__(self):
        self.api_key = settings.GEMINI_API_KEY
        self.model = settings.GEMINI_MODEL
        self.voice = settings.GEMINI_VOICE
        self.ws: websockets.WebSocketClientProtocol | None = None
        self.uri = (
            "wss://generativelanguage.googleapis.com/ws/"
            "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
            f"?key={self.api_key}"
        )
        # HA client for function call execution
        token = settings.effective_ha_token
        self.ha = HomeAssistantClient(settings.HA_URL, token) if token else None

    async def connect(self):
        """Establish the WebSocket connection to Gemini and send setup config."""
        logger.info(f"Connecting to Gemini Live API ({self.model})...")
        self.ws = await websockets.connect(self.uri)

        system_prompt = _load_system_prompt()

        setup_msg: dict = {
            "setup": {
                "model": self.model,
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {
                            "prebuiltVoiceConfig": {
                                "voiceName": self.voice
                            }
                        }
                    }
                },
                # Declare function calling tools
                "tools": HA_TOOLS,
            }
        }

        # Attach system prompt if available
        if system_prompt:
            setup_msg["setup"]["systemInstruction"] = {
                "parts": [{"text": system_prompt}]
            }

        await self.ws.send(json.dumps(setup_msg))

        # Wait for setup completion
        response_raw = await self.ws.recv()
        response = json.loads(response_raw)
        if "setupComplete" in response:
            logger.info("Gemini setup completed. System prompt and tools active.")
        else:
            logger.error(f"Setup failed: {response}")
            raise Exception("Gemini Live initial setup failed.")

    async def send_audio_chunk(self, pcm_bytes: bytes):
        """Send a raw PCM audio chunk to Gemini."""
        if not self.ws:
            return
        b64_audio = base64.b64encode(pcm_bytes).decode("utf-8")
        await self.ws.send(json.dumps({
            "realtimeInput": {
                "mediaChunks": [{"mimeType": "audio/pcm;rate=16000", "data": b64_audio}]
            }
        }))

    async def send_text(self, text: str):
        """Send a text input to Gemini (for debugging or non-audio sessions)."""
        if not self.ws:
            return
        await self.ws.send(json.dumps({
            "clientContent": {
                "turns": [{"role": "user", "parts": [{"text": text}]}],
                "turnComplete": True
            }
        }))

    async def _execute_tool_call(self, call_id: str, fn_name: str, args: dict) -> dict:
        """
        Executes a Gemini function call against Home Assistant and returns the result.
        """
        logger.info(f"🔧 Function call: {fn_name}({args})")

        if not self.ha:
            logger.warning("HA client not configured (HA_TOKEN missing). Cannot execute function calls.")
            return {"error": "Home Assistant integration not configured. Set HA_URL and HA_TOKEN."}

        try:
            if fn_name == "control_device":
                entity_id = args["entity_id"]
                action = args["action"]
                domain = entity_id.split(".")[0]

                service_data = {"entity_id": entity_id}
                if "brightness_pct" in args:
                    service_data["brightness_pct"] = args["brightness_pct"]
                if "color_temp_kelvin" in args:
                    service_data["color_temp_kelvin"] = args["color_temp_kelvin"]
                if "rgb_color" in args:
                    service_data["rgb_color"] = args["rgb_color"]
                if "position" in args:
                    service_data["position"] = args["position"]

                result = await self.ha.call_service(domain, action, service_data)
                return result

            elif fn_name == "get_device_state":
                state = await self.ha.get_state(args["entity_id"])
                # Return a slimmed-down version for Gemini (avoid context overflow)
                return {
                    "entity_id": state.get("entity_id"),
                    "state": state.get("state"),
                    "attributes": {
                        k: v for k, v in state.get("attributes", {}).items()
                        if k in ("friendly_name", "brightness", "color_temp_kelvin",
                                 "rgb_color", "temperature", "current_temperature",
                                 "unit_of_measurement", "device_class")
                    }
                }

            elif fn_name == "get_devices":
                domain_filter = args.get("domain")
                states = await self.ha.get_all_states()
                devices = []
                for state in states:
                    entity_id = state.get("entity_id", "")
                    domain = entity_id.split(".")[0] if "." in entity_id else ""
                    
                    if domain_filter and domain != domain_filter:
                        continue
                        
                    # If no specific domain requested, filter out noisy/internal HA domains
                    if not domain_filter and domain in ("automation", "script", "zone", "sun", "person", "update", "device_tracker", "binary_sensor"):
                        continue
                        
                    devices.append({
                        "entity_id": entity_id,
                        "name": state.get("attributes", {}).get("friendly_name", entity_id),
                        "state": state.get("state")
                    })
                
                logger.info(f"get_devices returned {len(devices)} entities (domain={domain_filter})")
                return {"devices": devices, "count": len(devices)}

            elif fn_name == "set_climate":
                entity_id = args["entity_id"]
                service_data = {"entity_id": entity_id}
                if "temperature" in args:
                    service_data["temperature"] = args["temperature"]

                results = []
                if "hvac_mode" in args:
                    results.append(await self.ha.call_service(
                        "climate", "set_hvac_mode",
                        {"entity_id": entity_id, "hvac_mode": args["hvac_mode"]}
                    ))
                if "temperature" in args:
                    results.append(await self.ha.call_service(
                        "climate", "set_temperature", service_data
                    ))
                return {"success": True, "results": results}

            elif fn_name == "save_memory":
                memory = args["memory"]
                category = args.get("category", "other")
                import datetime
                timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                entry = f"[{timestamp}] [{category.upper()}] {memory}\n"
                try:
                    with open(MEMORY_FILE, "a", encoding="utf-8") as f:
                        f.write(entry)
                    logger.info(f"💾 Memory saved: {entry.strip()}")
                    return {"success": True, "saved": entry.strip()}
                except Exception as e:
                    logger.error(f"Failed to save memory: {e}")
                    return {"error": str(e)}

            elif fn_name == "read_memories":
                try:
                    with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                        content = f.read().strip()
                    entries = [l for l in content.splitlines() if l.strip()]
                    logger.info(f"📖 Read {len(entries)} memories from file")
                    return {"memories": entries, "count": len(entries)}
                except FileNotFoundError:
                    return {"memories": [], "count": 0, "note": "No memories saved yet."}
                except Exception as e:
                    return {"error": str(e)}

            elif fn_name == "end_conversation":
                logger.info("👋 Gemini decided to end the conversation. Muting microphone.")
                set_bridge_active(False)
                return {"success": True, "note": "Conversation ended. Mic is now muted."}

            else:
                return {"error": f"Unknown function: {fn_name}"}

        except Exception as e:
            logger.error(f"Function call {fn_name} failed: {e}")
            return {"error": str(e)}

    async def receive_loop(self, on_audio_chunk: Callable[[bytes], Any]):
        """
        Listen for Gemini responses: audio chunks, text, and function calls.
        """
        if not self.ws:
            return

        try:
            async for message in self.ws:
                data = json.loads(message)

                # ── Audio / Text response ────────────────────────────────────
                if "serverContent" in data:
                    model_turn = data["serverContent"].get("modelTurn")
                    if model_turn:
                        for part in model_turn.get("parts", []):
                            if "text" in part:
                                logger.debug(f"Gemini text: {part['text']}")

                            if "inlineData" in part:
                                mime = part["inlineData"].get("mimeType", "")
                                if mime.startswith("audio/pcm"):
                                    pcm = base64.b64decode(part["inlineData"]["data"])
                                    if asyncio.iscoroutinefunction(on_audio_chunk):
                                        await on_audio_chunk(pcm)
                                    else:
                                        on_audio_chunk(pcm)

                    if data["serverContent"].get("turnComplete"):
                        logger.debug("Gemini turn complete.")

                # ── Function Call (tool use) ─────────────────────────────────
                elif "toolCall" in data:
                    tool_call = data["toolCall"]
                    function_calls = tool_call.get("functionCalls", [])
                    tool_responses = []

                    for fc in function_calls:
                        call_id = fc.get("id", "")
                        fn_name = fc.get("name", "")
                        fn_args = fc.get("args", {})

                        result = await self._execute_tool_call(call_id, fn_name, fn_args)
                        logger.info(f"🔧 Function {fn_name} result: {result}")

                        tool_responses.append({
                            "id": call_id,
                            "name": fn_name,
                            "response": {"output": result}
                        })

                    # Send all results back to Gemini
                    if tool_responses:
                        await self.ws.send(json.dumps({
                            "toolResponse": {
                                "functionResponses": tool_responses
                            }
                        }))

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"Gemini connection closed: {e}")
        except Exception as e:
            logger.error(f"Error in Gemini receive loop: {e}")

    async def close(self):
        """Close the Gemini WebSocket connection."""
        if self.ws:
            await self.ws.close()
            self.ws = None
            logger.info("Disconnected from Gemini.")
