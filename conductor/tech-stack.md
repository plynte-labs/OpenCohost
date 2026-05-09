# Tech Stack

## Language
- **Python 3.10+** — lenguaje principal

## UI
- **CustomTkinter** — framework de UI desktop (actual)
- **Tauri + React + Tailwind** — migración futura planificada

## LLM
- **Ollama** — inference local de modelos LLM
- Modelos soportados: llama3, gemma, qwen, mistral, entre otros (catálogo de 11)

## TTS
- **Edge-TTS** — motor ligero, voz cloud `es-MX-DaliaNeural`, 0% GPU
- **Qwen3-TTS 0.6B** — motor pesado local, clonación zero-shot con audio de referencia

## Audio
- **sounddevice** — captura de audio desde micrófono
- **soundfile** — lectura/escritura de archivos WAV
- **pygame** — reproducción de audio
- **numpy** — procesamiento de señal (RMS, etc.)

## Streaming / Chat
- **websockets** — conexión WebSocket a LiveAudio (transcripción Whisper)
- **pytchat** — ingestión de YouTube Live Chat
- **requests** — HTTP para APIs externas

## Backend Server
- **Flask** — servidor TTS local (puerto 5000)

## Data
- **SQLite** — persistencia de Smart Aggregator y Stream Admin
- **JSONL** — logs de chat y acciones de usuario
- **YAML** — configuración de subsistemas

## Seguridad
- **OAuth2** — flujo YouTube con token refresh automático
- **icacls** — hardening de permisos en archivos de tokens (Windows)

## Logging
- **logging** (stdlib) — logging estructurado con redacción de datos sensibles

## Hotkeys
- **pynput** — captura de hotkeys globales para Push-to-Talk

## Code Style
- **PEP 8** — convención de código Python
- **ruff** — linter recomendado (no configurado aún)
