import asyncio
import json
import logging
import websockets
from src.config import settings
from src.core.routines import routine_engine

logger = logging.getLogger("ha_events")

async def ha_websocket_listener():
    if not settings.HA_URL or not settings.effective_ha_token:
        logger.warning("HA WebSocket credentials missing. AI Routines will not trigger.")
        return

    # ws:// HTTP replacement logic
    url = settings.HA_URL.replace("http://", "ws://").replace("https://", "wss://")
    url = f"{url.rstrip('/')}/api/websocket"
    token = settings.effective_ha_token
    # Initial fetch for continuous mode
    try:
        from src.ha import HomeAssistantClient
        from src.core.session import Session
        client = HomeAssistantClient(settings.HA_URL, settings.effective_ha_token)
        state_data = await client.get_state("input_boolean.gemini_continuous_mode")
        if state_data and isinstance(state_data, dict) and state_data.get("state") == "on":
            Session.continuous_mode = True
            logger.info("Continuous Mode initialized as ON from HA.")
        else:
            Session.continuous_mode = False
    except Exception as e:
        logger.error(f"Failed to fetch initial continuous mode state: {e}")
        
    while True:
        try:
            async with websockets.connect(url) as ws:
                # 1. Wait for auth_required
                auth_req = await ws.recv()
                
                # 2. Send auth
                await ws.send(json.dumps({"type": "auth", "access_token": token}))
                auth_resp = await ws.recv()
                
                if json.loads(auth_resp).get("type") != "auth_ok":
                    logger.error(f"HA WS Auth failed: {auth_resp}")
                    return
                
                logger.info("HA WebSocket Connected & Authenticated. Listening for events...")
                
                # 3. Subscribe to state_changed events
                await ws.send(json.dumps({
                    "id": 1,
                    "type": "subscribe_events",
                    "event_type": "state_changed"
                }))
                
                # 4. Listen
                async for message in ws:
                    data = json.loads(message)
                    if data.get("type") == "event":
                        event = data.get("event", {})
                        if event.get("event_type") == "state_changed":
                            event_data = event.get("data", {})
                            entity_id = event_data.get("entity_id")
                            new_state = event_data.get("new_state", {})
                            old_state = event_data.get("old_state", {})
                            
                            if new_state and old_state:
                                if new_state.get("state") != old_state.get("state"):
                                    if entity_id == "input_boolean.gemini_continuous_mode":
                                        is_continuous = new_state.get("state") == "on"
                                        from src.core.session import Session
                                        if is_continuous != Session.continuous_mode:
                                            Session.continuous_mode = is_continuous
                                            logger.info(f"Gemini Continuous Mode is now {'ON' if is_continuous else 'OFF'}")
                                            if is_continuous:
                                                for session in list(Session.active_sessions):
                                                    if not session.is_active:
                                                        asyncio.create_task(session.activate())
                                            else:
                                                for session in list(Session.active_sessions):
                                                    if session.is_active:
                                                        session.deactivate()
                                    
                                    await routine_engine.evaluate_event(
                                        entity_id, 
                                        new_state.get("state"),
                                        new_state.get("attributes", {})
                                    )
                                    
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"HA WebSocket Event Error: {e}")
            await asyncio.sleep(5)
