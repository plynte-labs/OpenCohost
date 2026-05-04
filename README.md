# VoiceAI — Kira, tu co-host virtual para Stream

Aplicación de escritorio que crea un co-host de IA (**Kira**) corriendo 100% en local: sin internet, sin suscripciones, sin censura corporativa. Usa tu GPU para generar texto (LLM) y voz clonada (TTS) en tiempo real.

## Arquitectura

| Componente | Tecnología |
|---|---|
| **UI** | CustomTkinter (ventana desktop) |
| **LLM** | Ollama — 11 modelos en catálogo (default: `llama3`) |
| **TTS Ligero** | Edge-TTS — voz `es-MX-DaliaNeural`, 0% GPU |
| **TTS Pesado** | Qwen3-TTS 0.6B — clonación zero-shot (servidor Flask `:5000`) |
| **Audio** | sounddevice (grabar) + pygame (reproducir) |
| **Transcripción** | WebSocket → LiveAudio (Whisper) |

## Estructura del Proyecto

```
VoiceAI/
├── main.py              # Punto de entrada
├── server_qwen.py       # Servidor TTS multi-motor (Flask)
├── config/
│   ├── logger.py        # Logging estructurado (consola + archivo)
│   └── settings.py      # Constantes, catálogo de modelos, system prompt
├── core/
│   ├── llm_engine.py    # Motor IA: Ollama, memoria, pipeline TTS
│   └── profiles.py      # Carga/guardado de perfiles de personalidad
├── ui/
│   ├── app.py           # Interfaz principal (grabación, chat, WebSocket)
│   └── profiles_window.py  # Editor visual de perfiles
├── perfiles.json        # Perfiles de personalidad de la IA
├── Grabaciones/         # Audios de referencia grabados
├── legacy/              # Versiones anteriores del código
└── modelos_f5/          # Modelos descargados de HuggingFace
```

## Cómo Ejecutar

```powershell
# Terminal 1 — Servidor TTS (requiere xtts_env con PyTorch + Flask)
E:\Miniconda\envs\xtts_env\python.exe server_qwen.py

# Terminal 2 — Cliente (requiere flux_env con Ollama + sounddevice + websockets)
E:\Miniconda\envs\flux_env\python.exe main.py
```

## Features Actuales

-   **Chat manual**: escribe texto, la IA responde por voz
-   **Grabación de referencia**: captura tu voz y clónala con Qwen3-TTS
-   **2 motores TTS**: Ligero (Edge-TTS, 0% GPU) y Pesado (Qwen3-TTS, clonación)
-   **WebSocket Live**: conexión a transcripciones en vivo con reconexión automática
-   **Pipeline TTS por fragmentos**: la IA empieza a hablar apenas se genera la primera oración
-   **Memoria conversacional**: sliding window de 10 turnos
-   **Perfiles de personalidad**: 5 perfiles editables desde la UI
-   **Catálogo de modelos**: descarga y cambia entre 11 LLMs desde la interfaz
-   **Estados de UI**: indicadores visuales (procesando / listo), bloqueo de botones
-   **Logging estructurado**: archivos rotativos en `logs/`

## Roadmap (docs/changes.md)

### Por Implementar

-   **Push-to-Talk** con hotkey global (pynput)
-   **Silero VAD** como filtro de audio previo a Whisper
-   **Smart Chat Aggregator**: filtrado de mensajes cortos, análisis de sentimiento, triggers por actividad
-   **Indicadores de pipeline**: Escuchando / Procesando LLM / Sintetizando Voz
-   **Integración Twitch/YouTube API**: modificar metadata del stream
-   **Moderación automática**: Slow Mode / Emote Only basado en sentimiento
-   **RAG / Memoria a largo plazo**: ChromaDB para recordar streams anteriores
-   **Avatar visual**: fuente de navegador OBS con boca animada
-   **Entrenamiento de voz local**: finetuning de modelos VITS

## Requisitos de Hardware

-   **Mínimo**: GPU con 6 GB VRAM (ej. RTX 2060)
-   **Recomendado**: GPU con 12 GB VRAM (ej. RTX 3060) para correr LLM + TTS simultáneamente
-   **Servidor dedicado ideal**: GPU con 24 GB VRAM (ej. RTX 3090) + 64 GB RAM (ver `docs/futureserver.md`)

## Dependencias

**Cliente** (flux_env):
`customtkinter`, `ollama`, `sounddevice`, `soundfile`, `numpy`, `websockets`, `requests`, `pygame`, `edge-tts`

**Servidor TTS** (xtts_env):
`flask`, `torch`, `torchaudio`, `soundfile`, `qwen-tts`, `edge-tts`
