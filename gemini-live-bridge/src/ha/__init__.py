import aiohttp
import logging

logger = logging.getLogger("ha_client")

class HomeAssistantClient:
    """
    Async HTTP client for the Home Assistant REST API.
    Used by Gemini function calling to read/control HA entities.
    """
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def get_state(self, entity_id: str) -> dict:
        """
        Returns the current state of a HA entity.
        Example return: {"entity_id": "light.living_room", "state": "on", "attributes": {...}}
        """
        url = f"{self.base_url}/api/states/{entity_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    error = await resp.text()
                    logger.error(f"HA get_state failed for {entity_id}: {resp.status} {error}")
                    return {"error": f"HTTP {resp.status}: {error}"}

    async def set_state(self, entity_id: str, state: str, attributes: dict = None) -> dict:
        """
        Sets or creates the state of an entity in HA.
        """
        url = f"{self.base_url}/api/states/{entity_id}"
        data = {"state": state}
        if attributes:
            data["attributes"] = attributes
            
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=data) as resp:
                if resp.status in (200, 201):
                    return await resp.json()
                else:
                    error = await resp.text()
                    logger.error(f"HA set_state failed for {entity_id}: {resp.status} {error}")
                    return {"error": f"HTTP {resp.status}: {error}"}

    async def call_service(self, domain: str, service: str, data: dict) -> dict:
        """
        Calls a HA service (e.g. light.turn_on, switch.toggle).
        Returns list of affected states or error dict.
        """
        url = f"{self.base_url}/api/services/{domain}/{service}"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=data) as resp:
                if resp.status in (200, 201):
                    result = await resp.json()
                    logger.info(f"HA service call {domain}.{service} OK: {data}")
                    return {"success": True, "states": result}
                else:
                    error = await resp.text()
                    logger.error(f"HA call_service {domain}.{service} failed: {resp.status} {error}")
                    return {"success": False, "error": f"HTTP {resp.status}: {error}"}

    async def get_all_states(self) -> list:
        """Fetch all entity states — useful for grounding the AI in the current home state."""
        url = f"{self.base_url}/api/states"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    error = await resp.text()
                    logger.error(f"HA get_all_states failed: {resp.status} {error}")
                    return []
