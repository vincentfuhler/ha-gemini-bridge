"""
Gemini function declarations for Home Assistant control and AI memory.
These are sent to Gemini at session start so it knows what actions it can take.
"""

MEMORY_FILE = "/config/gemini_memories.txt"

HA_TOOLS = [
    {
        "functionDeclarations": [
            {
                "name": "control_device",
                "description": (
                    "Execute a service/action on a Home Assistant device. "
                    "Use entity IDs exactly as they appear in HA. "
                    "Standard actions: 'turn_on', 'turn_off', 'toggle'. "
                    "For covers/blinds/shutters: 'open_cover', 'close_cover', 'stop_cover', 'set_cover_position'. "
                    "For buttons: 'press'. For media players: 'media_play_pause', 'volume_up', 'volume_down'."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "entity_id": {
                            "type": "STRING",
                            "description": "The Home Assistant entity ID, e.g. 'light.living_room' or 'cover.shelly_window'"
                        },
                        "action": {
                            "type": "STRING",
                            "description": "The action/service to execute (turn_on, turn_off, toggle, open_cover, close_cover, set_cover_position, press, etc)"
                        },
                        "brightness_pct": {
                            "type": "NUMBER",
                            "description": "Optional: light brightness in percent (0-100)"
                        },
                        "position": {
                            "type": "NUMBER",
                            "description": "Optional: position for covers/blinds (0-100)"
                        },
                        "color_temp_kelvin": {
                            "type": "NUMBER",
                            "description": "Optional: light color temperature in Kelvin (e.g. 2700 for warm, 6500 for cool)"
                        },
                        "rgb_color": {
                            "type": "ARRAY",
                            "description": "Optional: light RGB color as [R, G, B] values 0-255",
                            "items": {"type": "NUMBER"}
                        }
                    },
                    "required": ["entity_id", "action"]
                }
            },
            {
                "name": "get_device_state",
                "description": (
                    "Get the current state of any Home Assistant entity. "
                    "Returns the state (e.g. 'on', 'off', temperature value) and attributes. "
                    "Use this to answer questions like 'Is the light on?' or 'What temperature is it?'"
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "entity_id": {
                            "type": "STRING",
                            "description": "The Home Assistant entity ID to query, e.g. 'sensor.living_room_temperature'"
                        }
                    },
                    "required": ["entity_id"]
                }
            },
            {
                "name": "get_devices",
                "description": (
                    "Get a list of available Home Assistant devices and entities. "
                    "Use this to discover what devices exist, their entity IDs, and their current states. "
                    "You can optionally filter by domain (e.g. 'light', 'switch', 'climate', 'sensor', 'media_player')."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "domain": {
                            "type": "STRING",
                            "description": "Optional: filter by domain (e.g. 'light', 'switch', 'climate')"
                        }
                    }
                }
            },
            {
                "name": "set_climate",
                "description": "Control a thermostat or climate device (set target temperature, HVAC mode).",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "entity_id": {
                            "type": "STRING",
                            "description": "Climate entity ID, e.g. 'climate.living_room'"
                        },
                        "temperature": {
                            "type": "NUMBER",
                            "description": "Target temperature in the unit configured in HA"
                        },
                        "hvac_mode": {
                            "type": "STRING",
                            "enum": ["heat", "cool", "heat_cool", "auto", "off"],
                            "description": "HVAC mode to set"
                        }
                    },
                    "required": ["entity_id"]
                }
            },
            {
                "name": "save_memory",
                "description": (
                    "Save a note or memory for future sessions. "
                    "Use this to remember user preferences, names, habits, facts, "
                    "or anything the user wants you to remember across conversations. "
                    "Examples: 'The user prefers dim warm light in the evening', "
                    "'The user's name is Vincent', 'The user wakes up at 7am on weekdays'. "
                    "Always confirm to the user that you saved the memory."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "memory": {
                            "type": "STRING",
                            "description": (
                                "A concise, factual note. Write in third person "
                                "(e.g. 'The user prefers...'). Be specific and actionable."
                            )
                        },
                        "category": {
                            "type": "STRING",
                            "enum": ["preference", "person", "routine", "device", "other"],
                            "description": "Category for organization"
                        }
                    },
                    "required": ["memory", "category"]
                }
            },
            {
                "name": "read_memories",
                "description": (
                    "Read all memories saved in previous sessions. "
                    "Use this only if you need to fetch memories that are not inherently provided in your instructions."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {}
                }
            },
            {
                "name": "end_conversation",
                "description": (
                    "End the current conversation and stop listening. "
                    "Call this tool when the user says goodbye, thanks you, or implies they no longer need assistance."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {}
                }
            }
        ]
    }
]
