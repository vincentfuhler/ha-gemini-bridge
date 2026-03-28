import asyncio
import aiohttp
import json
import os
import time
from src.logging import setup_logger
from src.config import settings
from src.ha import HomeAssistantClient

logger = setup_logger("optimizer")

class OptimizerService:
    def __init__(self):
        self.api_key = settings.GEMINI_API_KEY
        self.model = settings.OPTIMIZER_MODEL
        self.prompt = settings.OPTIMIZER_PROMPT
        self.output_file = "/config/optimized_devices.json"
        
        token = settings.effective_ha_token
        self.ha = HomeAssistantClient(settings.HA_URL, token) if token else None
        
        self._task = None

    def start(self):
        if not self.api_key or not self.ha:
            logger.warning("OptimizerService disabled: missing GEMINI_API_KEY or HA_TOKEN.")
            return
        
        self._task = asyncio.create_task(self._loop())
        logger.info("OptimizerService background task started.")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        while True:
            # Check if file was modified recently to prevent redundant runs on startup
            try:
                if os.path.exists(self.output_file):
                    file_age = time.time() - os.path.getmtime(self.output_file)
                    if file_age < 86400:
                        sleep_time = 86400 - file_age
                        logger.info(f"Devices were optimized recently. Sleeping {int(sleep_time)}s before next run.")
                        await asyncio.sleep(sleep_time)
            except Exception as e:
                logger.warning(f"Could not check file age: {e}")

            try:
                await self.run_optimization()
            except Exception as e:
                logger.error(f"Error in Optimizer loop: {e}")
            
            # Wait 24 hours (86400 seconds)
            await asyncio.sleep(86400)

    async def run_optimization(self) -> bool:
        """
        Gathers all Home Assistant devices and asks Gemini to structure them into JSON.
        Returns True if successful, False otherwise.
        """
        logger.info("Starting Home Assistant device optimization...")
        if not self.ha:
            logger.error("No HA client available.")
            return False

        try:
            states = await self.ha.get_all_states()
        except Exception as e:
            logger.error(f"Failed to fetch HA states: {e}")
            return False

        # Filter and summarize the massive state dump to save tokens
        raw_devices = []
        for state in states:
            entity_id = state.get("entity_id", "")
            domain = entity_id.split(".")[0] if "." in entity_id else ""
            
            # Skip very noisy or irrelevant internal domains
            if domain in ("automation", "script", "zone", "sun", "person", "update", "device_tracker"):
                continue
                
            raw_devices.append({
                "entity_id": entity_id,
                "name": state.get("attributes", {}).get("friendly_name", entity_id),
                "state": state.get("state"),
                "domain": domain
            })

        devices_json = json.dumps(raw_devices, ensure_ascii=False)
        logger.info(f"Gathered {len(raw_devices)} devices to analyze.")

        # Prepare Gemini Request
        url = f"https://generativelanguage.googleapis.com/v1beta/{self.model}:generateContent?key={self.api_key}"
        
        payload = {
            "systemInstruction": {
                "parts": [{"text": "You are a smart home configuration optimizer. You MUST return ONLY valid JSON without any markdown formatting wrappers (no ```json). Do not add any conversational text."}]
            },
            "contents": [{
                "parts": [
                    {"text": self.prompt + "\n\nHere are the raw Home Assistant devices:\n" + devices_json}
                ]
            }],
            "generationConfig": {
                "responseMimeType": "application/json"
            }
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(f"Gemini API returned {resp.status}: {error_text}")
                        return False
                        
                    data = await resp.json()
                    
                    if "candidates" not in data or not data["candidates"]:
                        logger.error("Gemini returned no candidates.")
                        return False
                        
                    result_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                    
                    # Ensure it parses as JSON
                    optimized_json = json.loads(result_text)
                    
                    # Save to file
                    with open(self.output_file, "w", encoding="utf-8") as f:
                        json.dump(optimized_json, f, indent=2, ensure_ascii=False)
                        
                    logger.info(f"✨ Successfully optimized devices! Saved to {self.output_file}")
                    return True
                    
        except json.JSONDecodeError as je:
            logger.error(f"Gemini did not return valid JSON: {je}")
            return False
        except Exception as e:
            logger.error(f"Failed to optimize devices via Gemini: {e}")
            return False

optimizer_service = OptimizerService()
