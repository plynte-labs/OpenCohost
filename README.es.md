# OpenCohost — Co-host de IA para streaming, local-first

OpenCohost es una plataforma de co-host de IA para streaming que funciona completamente en tu hardware. El producto central es **Kira**, una co-host de IA con personalidad definida (sarcasmo seco, humor afilado) que usa Ollama y modelos de TTS locales. Sin suscripciones a la nube. Sin latencias inesperadas durante el stream.

> English version: [README.md](README.md)

## Qué hace Kira

- Escucha tu stream a través de un feed de transcripción local por WebSocket
- Responde por voz usando inferencia LLM local (Ollama) y TTS local
- Mantiene una ventana de conversación deslizante durante la sesión de stream
- Alterna entre un motor de TTS liviano (Edge-TTS, 0% GPU) y un motor de voz local de alta calidad (Qwen3-TTS con clonación zero-shot)
- Monitorea su propio estado: si el TTS deja de estar disponible, Kira degrada de forma controlada en lugar de crashear

## Características

| Característica | Estado |
|---|---|
| Chat manual con respuesta por voz | Estable |
| Push-to-talk con hotkey global | Estable |
| TTS liviano (Edge-TTS) | Estable |
| TTS local (Qwen3-TTS, clonación zero-shot) | Estable |
| Fallback de TTS offline con Piper | Estable |
| Feed de transcripción en vivo por WebSocket | Estable |
| Pipeline TTS por fragmentos (Kira habla antes de que termine el LLM) | Estable |
| Memoria conversacional (ventana deslizante de 10 turnos) | Estable |
| Perfiles de personalidad (editables desde la UI) | Estable |
| Catálogo de modelos LLM con cambio de modelo en un clic | Estable |
| Gestión del ciclo de vida de Ollama desde la UI | Estable |
| Agregador de chat inteligente (YouTube Live) | Estable |
| Panel Stream Admin (YouTube OAuth, placeholder de Twitch) | MVP |
| Monitor de salud con fallback de TTS | Estable |
| Modo compacto para streaming en monitor secundario | Estable |

## Requisitos

### Hardware

| Nivel | VRAM de GPU | Caso de uso |
|---|---|---|
| Mínimo | 6 GB | Solo Edge-TTS; Ollama con modelo pequeño |
| Recomendado | 12 GB | LLM + Qwen3-TTS concurrentes |
| Ideal | 24 GB | GPU dedicada para servidor; Ollama + TTS completo sin contención |

### Software

- Python 3.13 (entorno conda o venv activado)
- [Ollama](https://ollama.com/) instalado y en ejecución
- `customtkinter`, `ollama`, `sounddevice`, `soundfile`, `numpy`, `websockets`, `requests`, `pygame`, `edge-tts`, `pytchat` (entorno principal)
- `flask`, `torch`, `torchaudio`, `soundfile`, `qwen-tts`, `edge-tts` (entorno del servidor TTS, opcional — solo si usas Qwen3-TTS)

## Configuración

> Estos comandos asumen que tu entorno Python ya está activado. Reemplaza `python` con la ruta al intérprete de tu entorno si no estás en una shell activada.

```powershell
# Instalar dependencias principales
python -m pip install -r requirements.txt

# (Opcional) Instalar dependencias del servidor TTS en un entorno separado
# python -m pip install flask torch torchaudio soundfile qwen-tts edge-tts
```

### Ejecutar

```powershell
# Terminal 1 — aplicación principal (LLM + UI + pipeline de audio)
python main.py

# Terminal 2 — servidor Qwen3-TTS (solo necesario para clonación de voz local de alta calidad)
# Ejecutar en el entorno que tiene torch + qwen-tts instalados
python server_qwen.py
```

Kira usará Edge-TTS (liviano, en línea) si el servidor Qwen3-TTS no está disponible.

### Configurar la ruta del intérprete TTS

Si usas Qwen3-TTS, configura la variable de entorno `XTTS_PYTHON` apuntando al intérprete Python de tu entorno TTS:

```powershell
$env:XTTS_PYTHON = "ruta/a/tu/entorno-tts/python.exe"
python main.py
```

O establece `tools.xtts_python` en `config/storage.yaml`.

### Configurar rutas de almacenamiento

Para mover los modelos de Ollama, caché o archivos temporales a una unidad diferente, edita `config/storage.yaml`:

```yaml
storage:
  # cache_root: "/ruta/a/tu/cache"
  # temp_root: "/ruta/a/tu/temp"
  # ollama_models: "/ruta/a/tus/modelos/ollama"
```

## Arquitectura

```
OpenCohost/
├── main.py               # Punto de entrada
├── server_qwen.py        # Servidor TTS multi-motor (Flask)
├── config/
│   ├── logger.py         # Logging estructurado (consola + archivos rotativos)
│   ├── settings.py       # Constantes, catálogo de modelos, system prompt
│   ├── storage.py        # Resolución de rutas portables para cache/temp
│   └── storage.yaml      # Overrides de rutas de almacenamiento
├── core/
│   ├── llm_engine.py     # Orquestación LLM, memoria, pipeline TTS
│   ├── health_monitor.py # Salud de servicios, fallback de TTS
│   └── profiles.py       # Carga/guardado de perfiles de personalidad
├── ui/
│   ├── app_shell.py      # Shell principal de UI (UIState observer thread-safe)
│   └── model_panel.py    # Panel de gestión de modelos
├── smart_aggregator/     # Agregador de YouTube Live Chat (RF3)
├── stream_admin/         # Panel Stream Admin: OAuth, metadata, moderación (RF4)
└── config/
    ├── default_profiles.json  # Perfiles de personalidad predeterminados
    └── stream_admin.yaml      # Configuración de Stream Admin
```

## Perfiles de personalidad

En el primer arranque, OpenCohost genera un conjunto de perfiles de personalidad predeterminados en tu `perfiles.json` local (ignorado por git):

| Perfil | Persona |
|---|---|
| Akira | Co-host por defecto — equilibrada y afilada |
| Akira (Learn) | Modo aprendizaje — educativo y alentador |
| Comunidad | Modo comunidad — cálido e inclusivo |
| Calmado | Modo calmado — ritmo más lento, tono sereno |
| Técnico | Modo técnico — preciso, seco, cínico |
| Show | Modo alto impacto — enérgico y performático |

El nombre Kira y la personalidad base se preservan en todos los perfiles.

## Integración con Ollama

La aplicación valida Ollama al iniciar y desactiva las acciones que dependen del LLM si el servicio no está disponible. El panel de modelos ofrece un único botón contextual:

- **Instalar Ollama** — abre la página oficial de descarga cuando no se encuentra el binario
- **Iniciar Ollama** — ejecuta `ollama serve` cuando está instalado pero no responde
- **Descargar modelo** — descarga el modelo seleccionado cuando Ollama está listo pero el modelo no existe localmente
- **Activar modelo** — cambia al modelo seleccionado cuando ya está instalado
- **Instalar dependencia Python** — avisa cuando falta el paquete `ollama` en el entorno Python activo

El servicio se verifica en `http://127.0.0.1:11434/api/tags`. Si Ollama deja de estar disponible durante una sesión, el motor se marca como no listo y bloquea el procesamiento, el cambio de modelo y las descargas hasta que el servicio vuelva a responder.

## Stream Admin (RF4)

El panel Stream Admin soporta OAuth/API de YouTube para leer y escribir chat, metadata, moderación y analíticas. Se incluye un placeholder de proveedor Twitch. Las credenciales OAuth se guardan en `data/stream_admin/oauth_client.json` (ignorado por git). También se soportan las variables de entorno `YOUTUBE_OAUTH_CLIENT_ID` y `YOUTUBE_OAUTH_CLIENT_SECRET`.

## Tests

```powershell
# Recolección completa de tests
python -m pytest --collect-only -q

# Suite focalizada en modelos y recuperación
python -m pytest tests/test_llm_tiers.py tests/test_model_panel.py tests/test_heavy_model_inference_recovery.py -q

# Suite del monitor de salud
python -m pytest tests/test_health_monitor.py tests/test_health_integration.py tests/test_app_shell_obs_resilience.py -q
```

Ver [docs/TESTING.md](docs/TESTING.md) para la superficie completa de tests.

## Licencia

Este proyecto está bajo la Licencia MIT. Ver [LICENSE](LICENSE) para el texto completo.
