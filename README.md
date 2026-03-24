# Home Assistant Gemini Live Bridge

Ein eigener Bridge-Service für Home Assistant Voice, der per WebSocket mit Home Assistant kommuniziert und im Hintergrund das asynchrone Streaming der Gemini Live API nutzt, um nahezu latenzfreie Sprachinteraktionen zu erzielen.

## Architektur

Der Service fungiert als Vermittler (Bridge) zwischen Home Assistant (z.B. Atom Echo oder anderen Wyoming-Protokoll tauglichen Geräten) und der neuen Gemini Bidi / Live API. 
Es streamt eingehende Audio-Chunks (Microphone Data) direkt an Gemini weiter und liefert die Audio-Antwort-Chunks reibungslos zurück an Home Assistant.

### Erwartetes Audio Format
Gemini Multimodal Live erwartet die Audio-Daten als raw PCM, im Normalfall:
- **Sample Rate:** `16000` Hz (Alternativ `24000` Hz aber 16kHz ist HA Standard)
- **Channels:** `1` (Mono)
- **Bit Depth:** `16-bit`
Senden Sie am WebSocket `/ws` die reinen Binärdaten der PCM Chunks.

## Voraussetzungen & Installation

### Option 1: Home Assistant Add-on (Empfohlen für HAOS / Supervisor)

1. Gehe in Home Assistant zu **Einstellungen -> Add-ons -> Add-on Store**.
2. Klicke oben rechts auf die drei Punkte und wähle **Repositories**.
3. Füge die URL dieses GitHub-Repositories hinzu und klicke auf hinzufügen.
4. Lade die Seite neu (bzw. fahre ganz nach unten) und suche nach **Gemini Live Bridge**.
5. Klicke auf *Installieren*. Das Bauen des Containers kann ein paar Minuten dauern.
6. Wechsle nach der Installation zum Reiter **Konfiguration** und trage deinen `GEMINI_API_KEY` ein.
7. Starte das Add-on und wechsle in den Reiter **Protokolle**, um zu prüfen, ob der Uvicorn-Server läuft.

### Option 2: Docker (Standalone)

1. Repo klonen
2. Datei `.env.example` in `.env` kopieren: `cp .env.example .env`
3. Ersetzen Sie in der `.env` den Parameter `GEMINI_API_KEY` mit Ihrem realen Key.
4. Container bauen und starten:
   ```bash
   docker-compose up -d --build
   ```

### Option 3: Lokal (Python 3.11+)


1. Virtuelles Environment anlegen: `python -m venv venv`
2. Environment aktivieren: `source venv/bin/activate` (Mac/Linux)
3. Abhängigkeiten installieren: `pip install -r requirements.txt`
4. `.env` anlegen
5. Server starten: `uvicorn src.main:app --host 0.0.0.0 --port 8000`

## Integration in Home Assistant & Hardware

Um das Ganze in Home Assistant zu nutzen, umgehen wir die klassische Text-Pipeline, damit Gemini Live in Echtzeit den Tonfall und Unterbrechungen (Full Duplex) analysieren kann.
Der Dienst läuft nun unter `ws://<DEINE_HA_IP>:8000/ws`.

### Hardware Anbindung (Empfohlen: ESPHome & M5Stack Atom Echo)
Wenn du eine smarte Lautsprecher-Hardware wie den **M5Stack Atom Echo** nutzt, kannst du das Mikrofon direkt mit diesem Websocket verbinden.
Dafür gibt es im Ordner `esphome-client/` eine vorgefertigte Konfiguration!
1. Öffne dein ESPHome Dashboard in Home Assistant.
2. Erstelle ein neues Gerät und kopiere den Inhalt der `esphome-client/atom_echo.yaml` hinein.
3. Lege die Datei `esphome-client/gemini_websocket.h` in denselben Ordner wie deine ESPHome YAMLs (unter `/config/esphome/`).
4. Ändere die IP-Adresse in der YAML zu der IP deines Add-ons und flashe das Gerät!

### Software Anbindung (Node-RED etc.)
- Er kann in Node-RED angebunden werden, indem ein Audio-Stream (16kHz, Mono, PCM 16-bit) dorthin als Websocket Payload gesendet wird.
- Teil-Implementierung über `custom_components` ist ebenfalls möglich, wenn Sie eine Intercom/Conversation API schreiben, die an diese URL streamt. 

Ein Beispiel für das Scripting über Python-Clients in HA (z.B. AppDaemon / pyscript):
```python
import websockets

async def stream_audio_to_gemini(audio_source):
    async with websockets.connect("ws://<BRIDGE_IP>:8000/ws") as ws:
        # Puffer für Mikrofon-Daten in den Websocket schießen
        async for chunk in audio_source:
            await ws.send(chunk)
            
        # Parallel Audio Empfangen
        while True:
            response = await ws.recv()
            # an HA Audioausgang (TTS Playback pipeline) iterativ anfügen
            play_audio_chunk(response)
```

## Endpoints

- **`/health` (GET):** Health-Status abfragen
- **`/ws` (WebSocket):** Der Hauptendpunkt für den bidirektionalen Audio-Stream

## Struktur

- `src/api/` FastAPI Routen & Websockets
- `src/core/` Audio Config und Session-Management (Duplex Streaming Logik)
- `src/gemini/` Async Worker für die Gemini Live (BidiGenerateContent) API.
- `src/config.py` Env Einstellungen
- `src/main.py` Einstiegspunkt

