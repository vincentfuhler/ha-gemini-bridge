import os
import json
import logging
import asyncio
import aiohttp
from src.config import settings
from src.gemini.tools import HA_TOOLS
from src.ha import HomeAssistantClient

logger = logging.getLogger("ai_routines")

ROUTINES_FILE = "/config/ai_routines.json"
# Fallback for dev environment without /config
if not os.path.exists("/config"):
    ROUTINES_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "ai_routines.json")

class RoutineEngine:
    def __init__(self):
        self.routines = []
        token = settings.effective_ha_token
        self.ha = HomeAssistantClient(settings.HA_URL, token) if token else None
        self.load_routines()

    def load_routines(self):
        try:
            if os.path.exists(ROUTINES_FILE):
                with open(ROUTINES_FILE, "r", encoding="utf-8") as f:
                    self.routines = json.load(f)
                logger.info(f"Loaded {len(self.routines)} AI routines.")
            else:
                self.routines = []
        except Exception as e:
            logger.error(f"Failed to load routines: {e}")
            self.routines = []

    def save_routine(self, trigger_entity: str, trigger_state: str, action_prompt: str) -> bool:
        new_routine = {
            "trigger_entity": trigger_entity,
            "trigger_state": trigger_state,
            "action_prompt": action_prompt
        }
        self.routines.append(new_routine)
        try:
            os.makedirs(os.path.dirname(ROUTINES_FILE), exist_ok=True)
            with open(ROUTINES_FILE, "w", encoding="utf-8") as f:
                json.dump(self.routines, f, indent=4)
            logger.info(f"Saved new AI routine: {new_routine}")
            return True
        except Exception as e:
            logger.error(f"Failed to save routine: {e}")
            return False

    async def evaluate_event(self, entity_id: str, new_state: str, attributes: dict):
        """Called by the HA WebSocket listener when an entity state changes."""
        for r in self.routines:
            if r.get("trigger_entity") == entity_id and r.get("trigger_state") == new_state:
                logger.info(f"🚀 AI Routine triggered for {entity_id} == {new_state}")
                asyncio.create_task(self.invoke_agent(r, attributes))

    async def invoke_agent(self, routine: dict, attributes: dict):
        """Invoke Gemini via REST API to execute the routine's instructions."""
        api_key = settings.GEMINI_API_KEY
        if not api_key:
            return
            
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
        
        prompt = (
            f"You are an AI Smart Home Controller running as a background routine.\n"
            f"A trigger just occurred: The entity '{routine['trigger_entity']}' changed state to '{routine['trigger_state']}'.\n"
            f"Entity attributes: {json.dumps(attributes)}\n\n"
            f"The user wrote this instruction for the routine:\n"
            f"\"{routine['action_prompt']}\"\n\n"
            f"Please execute the necessary Home Assistant tools to fulfill this instruction. Do not write markdown or conversational replies, just invoke the function calls."
        )

        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            "tools": HA_TOOLS
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload, headers={"Content-Type": "application/json"}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            parts = candidates[0].get("content", {}).get("parts", [])
                            for part in parts:
                                if "functionCall" in part:
                                    fc = part["functionCall"]
                                    await self._execute_tool(fc.get("name"), fc.get("args", {}))
                    else:
                        error_text = await resp.text()
                        logger.error(f"Gemini Routine execution failed: {resp.status} - {error_text}")
            except Exception as e:
                logger.error(f"Error invoking routine agent: {e}")

    async def _execute_tool(self, fn_name: str, args: dict):
        """Execute a tool call using the REST HA client."""
        logger.info(f"🤖 AI Routine Tool Call: {fn_name}({args})")
        if not self.ha:
            return
            
        try:
            if fn_name == "control_device":
                entity_id = args["entity_id"]
                action = args["action"]
                domain = entity_id.split(".")[0]
                
                service_data = {"entity_id": entity_id}
                for key in ["brightness_pct", "color_temp_kelvin", "rgb_color", "position"]:
                    if key in args:
                        service_data[key] = args[key]

                await self.ha.call_service(domain, action, service_data)
                
            elif fn_name == "create_group":
                service_data = {
                    "object_id": args["group_id"],
                    "name": args["name"],
                    "entities": args["entities"]
                }
                await self.ha.call_service("group", "set", service_data)

            elif fn_name == "set_climate":
                # Implementation matching client.py
                entity_id = args["entity_id"]
                service_data = {"entity_id": entity_id}
                if "temperature" in args:
                    service_data["temperature"] = args["temperature"]

                if "hvac_mode" in args:
                    await self.ha.call_service("climate", "set_hvac_mode", {"entity_id": entity_id, "hvac_mode": args["hvac_mode"]})
                if "temperature" in args:
                    await self.ha.call_service("climate", "set_temperature", service_data)
                    
        except Exception as e:
            logger.error(f"Routine tool execution {fn_name} failed: {e}")

routine_engine = RoutineEngine()
