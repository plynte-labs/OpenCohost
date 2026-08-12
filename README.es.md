# OpenCohost — Co-host de IA para streaming, local-first

OpenCohost es una plataforma de co-host de IA para streaming. El producto central es **Kira**, una co-host de IA con personalidad definida (sarcasmo seco, humor afilado). Kira utiliza un **motor LLM local mediante Ollama** y **voz en la nube gratuita (Microsoft Edge-TTS) en v1**. El chat de espectadores, los prompts y la memoria conversacional nunca salen de tu equipo — solo el texto hablado de salida de Kira se envía a Edge-TTS para la síntesis de voz. La voz local de alta fidelidad está planificada como opción avanzada en una versión futura.

> English version: [README.md](README.md)

## Qué hace Kira

- Escucha tu stream a través de un feed de transcripción local por WebSocket
- Responde por voz usando inferencia LLM local (Ollama) y Edge-TTS (voz en la nube gratuita de Microsoft)
- Mantiene una ventana de conversación deslizante durante la sesión de stream
- Admite TTS sin conexión opcional mediante Piper (extra `local-tts`) — sin llamadas a la nube cuando está activo
- Monitorea su propio estado: si el TTS deja de estar disponible, Kira degrada de forma controlada en lugar de crashear

## Características

| Característica | Estado |
|---|---|
| Chat manual con respuesta por voz | Estable |
| Push-to-talk con hotkey global | Estable |
| TTS predeterminado (Edge-TTS, voz en la nube gratuita de Microsoft) | Estable |
| TTS offline opcional (Piper, extra `local-tts`) | Estable |
| Feed de transcripción en vivo por WebSocket | Estable |
| Pipeline TTS por fragmentos (Kira habla antes de que termine el LLM) | Estable |
| Memoria conversacional (ventana deslizante de 10 turnos) | Estable |
| Perfiles de personalidad (editables desde la UI) | Estable |
| Catálogo de modelos LLM con cambio de modelo en un clic | Estable |
| Gestión del ciclo de vida de Ollama desde la UI | Estable |
| Agregador de chat inteligente (Twitch) | Estable |
| Agregador de chat inteligente (YouTube) | Opcional, no oficial — ver [PRIVACY.md](docs/PRIVACY.md#youtube-live-chat-is-opt-in-and-unofficial) |
| Monitor de salud con fallback de TTS | Estable |
| Modo compacto para streaming en monitor secundario | Estable |

> **Hoja de ruta (no incluido en v1):** La voz local de alta fidelidad mediante Qwen3-TTS / F5 con clonación de voz está planificada como opción avanzada en una versión futura. Requiere un entorno Python separado, ~2 GB de modelos y una GPU con VRAM suficiente.

## Requisitos

### Requisitos previos

- **Windows** (plataforma principal compatible; validación en máquina limpia para Linux/macOS en curso)
- **Python 3.10+** (entorno conda o venv activado)
- **[Ollama](https://ollama.com/) instalado y en ejecución** — Kira no puede iniciar sin él

### Hardware

| Nivel | VRAM de GPU | Caso de uso |
|---|---|---|
| Mínimo | 6 GB | Edge-TTS (nube); Ollama con modelo pequeño |
| Recomendado | 8–12 GB | Inferencia LLM cómoda con margen |
| Ideal | 16+ GB | Modelos más grandes sin contención |

## Configuración

> Estos comandos asumen que tu entorno Python ya está activado. Reemplaza `python` con la ruta al intérprete de tu entorno si no estás en una shell activada.

```powershell
# Instalar el paquete con los extras predeterminados (Edge-TTS + integraciones de plataforma)
pip install -e ".[cloud-tts,integrations]"

# Equivalente con uv
uv pip install -e ".[cloud-tts,integrations]"

# Opcional: agregar soporte de TTS offline con Piper (sin llamadas a la nube para síntesis de voz)
pip install -e ".[cloud-tts,integrations,local-tts]"
```

**Referencia de extras:**

| Extra | Qué habilita |
|---|---|
| `cloud-tts` | Edge-TTS — voz en la nube gratuita de Microsoft (predeterminado) |
| `local-tts` | Piper TTS offline — completamente local, sin llamadas a la nube |
| `integrations` | OBS WebSocket, monitor de VRAM NVIDIA |
| `youtube-chat` | Chat de YouTube no oficial (pytchat) — opcional, [leer esto primero](docs/PRIVACY.md#youtube-live-chat-is-opt-in-and-unofficial) |
| `dev` | pytest, pre-commit, detect-secrets |

### Ejecutar

```powershell
python -m opencohost
```

### Configurar rutas de almacenamiento

Para mover los modelos de Ollama, caché o archivos temporales a una unidad diferente, edita `opencohost/config/storage.yaml`:

```yaml
storage:
  # cache_root: "/ruta/a/tu/cache"
  # temp_root: "/ruta/a/tu/temp"
  # ollama_models: "/ruta/a/tus/modelos/ollama"
```

## Arquitectura

```
opencohost/
├── __main__.py           # Punto de entrada (python -m opencohost)
├── config/
│   ├── logger.py         # Logging estructurado (consola + archivos rotativos)
│   ├── settings.py       # Constantes, catálogo de modelos, system prompt
│   ├── storage.py        # Resolución de rutas portables para cache/temp
│   ├── storage.yaml      # Overrides de rutas de almacenamiento
│   └── default_profiles.json  # Perfiles de personalidad predeterminados
├── core/
│   ├── llm_engine.py     # Orquestación LLM, memoria, pipeline TTS
│   ├── health_monitor.py # Salud de servicios, fallback de TTS
│   └── profiles.py       # Carga/guardado de perfiles de personalidad
├── ui/
│   ├── app_shell.py      # Shell principal de UI (UIState observer thread-safe)
│   └── model_panel.py    # Panel de gestión de modelos
└── smart_aggregator/     # Agregador de chat en vivo (Twitch; YouTube opcional)
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
