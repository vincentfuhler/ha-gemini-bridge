import asyncio
import websockets
import json
import os

API_KEY = "dummy" # We can just test without a valid API key, it will give a 400 or 403 API key invalid, OR if the URI is wrong it will give 404!
# Wait, I need a valid API key to get to the model check phase.
