# VoiceAI — Kira, tu co-host virtual para Stream v1.0.0

Aplicación de escritorio que crea un co-host de IA (**Kira**) con procesamiento local-first: LLM y TTS pesado corren en tu GPU sin suscripciones ni censura corporativa. Algunas funciones opcionales (Edge-TTS, YouTube Chat, Stream Admin) usan Internet.

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
├── smart_aggregator/    # RF3: agregador inteligente de chat YouTube Live
├── ui/
│   ├── app.py           # Interfaz principal (Kira, configuración, Stream Admin, logs)
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
-   **Gestión de Ollama desde la UI**: detecta si Ollama falta, está apagado o listo; el mismo botón permite instalar, iniciar, descargar o activar modelos
-   **Estados de UI**: indicadores visuales (procesando / listo), bloqueo de botones
-   **Logging estructurado**: archivos rotativos en `logs/`
-   **Smart Chat Aggregator RF3**: conexión a YouTube Live Chat, filtros, anti-spam, vibe, triggers y pestaña `YT Chat`
-   **Stream Admin RF4 MVP**: pestaña `Stream Admin`, YouTube OAuth/API preparado, Twitch placeholder, metadata, moderación, analíticas y mensajes al chat bajo permisos
-   **UI/UX refactor seguro**: vista principal `Kira`, configuración lateral tabulada, `Stream Admin` administrativo y logs inferiores bajo `Mostrar logs`

## Roadmap (docs/changes.md)

### Implementados

-   **Push-to-Talk** con hotkey global (pynput) ✅
-   **Indicadores de pipeline**: Escuchando / Procesando LLM / Sintetizando Voz ✅
-   **Modo compacto** para minimizar espacio en monitor ✅
-   **Panel de acciones Kira** con persistencia y mensajes simulados ✅
-   **Smart Chat Aggregator** para YouTube Live Chat: filtrado, vibe, triggers, historial y UI separada ✅
-   **RF4 Stream Admin MVP base**: módulo `stream_admin/`, UI, config YAML, OAuth local seguro MVP, YouTube provider y Twitch placeholder ✅
-   **Refactor UI/UX seguro**: jerarquía visual centrada en Kira, estados operativos claros y Stream Admin secundario ✅

### Por Implementar

-   **Silero VAD** como filtro de audio previo a Whisper
-   **Hardening RF4 OAuth/tokens**: migrar tokens locales a keyring/Windows Credential Manager y ampliar validaciones live reales
-   **RAG / Memoria a largo plazo**: ChromaDB para recordar streams anteriores
-   **Avatar visual**: fuente de navegador OBS con boca animada
-   **Entrenamiento de voz local**: finetuning de modelos VITS

## Requisitos de Hardware

-   **Mínimo**: GPU con 6 GB VRAM (ej. RTX 2060)
-   **Recomendado**: GPU con 12 GB VRAM (ej. RTX 3060) para correr LLM + TTS simultáneamente
-   **Servidor dedicado ideal**: GPU con 24 GB VRAM (ej. RTX 3090) + 64 GB RAM (ver `docs/futureserver.md`)

## Dependencias

**Cliente** (flux_env):
`customtkinter`, `ollama`, `sounddevice`, `soundfile`, `numpy`, `websockets`, `requests`, `pygame`, `edge-tts`, `pytchat`

## Ollama y Modelos

La app valida Ollama al iniciar y mantiene desactivadas las acciones que dependen del LLM si el servicio no está disponible. En la configuración de modelos hay un único botón contextual:

-   `Instalar Ollama`: abre la página oficial de descarga cuando no se encuentra el binario de Ollama.
-   `Iniciar Ollama`: intenta ejecutar `ollama serve` cuando Ollama está instalado pero el servicio local no responde.
-   `Descargar modelo`: descarga el modelo seleccionado cuando Ollama está listo pero el modelo no existe localmente.
-   `Activar modelo`: cambia al modelo seleccionado cuando ya está instalado.
-   `Instalar dependencia Python`: avisa que falta el paquete `ollama` en el entorno Python activo.

El servicio se comprueba contra `http://127.0.0.1:11434/api/tags`. Si Ollama deja de estar disponible, el motor IA marca el estado como no listo y bloquea procesamiento, cambio de modelo y descargas hasta que el servicio vuelva a responder.

**RF4 OAuth YouTube**:
OAuth/API oficial de YouTube usa `requests` y stdlib en el MVP. Las credenciales pueden guardarse desde la pestaña `Stream Admin` (`Client ID`, `Secret`, `Guardar OAuth`) y quedan en `data/stream_admin/oauth_client.json`, ignorado por git. También se soportan `YOUTUBE_OAUTH_CLIENT_ID` y `YOUTUBE_OAUTH_CLIENT_SECRET` por variables de entorno.

**Servidor TTS** (xtts_env):
`flask`, `torch`, `torchaudio`, `soundfile`, `qwen-tts`, `edge-tts`

## Licencia

Este proyecto está bajo la Licencia **MIT**. Consulta el archivo [LICENSE](file:///e:/VoiceAI/LICENSE) para ver el texto completo.
