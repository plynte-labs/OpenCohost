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

### Requisitos de Tauri

La UI del producto (`pnpm tauri:debug`) necesita un toolchain de Rust/Node además de lo anterior:

| Requisito | Notas |
|---|---|
| [Node.js](https://nodejs.org/) | Cualquier LTS actual |
| [pnpm](https://pnpm.io/) `11.5.2` | Versión fijada vía `packageManager` en `package.json`; `corepack enable` la detecta automáticamente |
| [Rust + Cargo](https://rustup.rs/) | No hay `rust-toolchain.toml` en este repo — la versión de Rust no está fijada, cualquier toolchain estable reciente funciona |
| [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) | Windows 11 lo incluye por defecto; instalar manualmente en Windows 10 |
| MSVC Build Tools (workload de C++) | Necesario para el target MSVC de `rustc` en Windows — instalar vía [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/) |

### Hardware

| Nivel | VRAM de GPU | Caso de uso |
|---|---|---|
| Mínimo | 6 GB | Edge-TTS (nube); Ollama con modelo pequeño |
| Recomendado | 8–12 GB | Inferencia LLM cómoda con margen |
| Ideal | 16+ GB | Modelos más grandes sin contención |

## Configuración

Cloná con submódulos: el front end de Tauri es su propio repositorio y está
enlazado en `OpenCohost_UI/`. Un `git clone` sin la bandera deja ese directorio
vacío, y la sección «Ejecutar» de más abajo no tiene a dónde entrar.

```powershell
git clone --recursive https://github.com/plynte-labs/opencohost.git
cd opencohost

# ¿ya lo clonaste sin la bandera?
git submodule update --init --recursive
```

> Estos comandos asumen que tu entorno Python ya está activado. Reemplaza `python` con la ruta al intérprete de tu entorno si no estás en una shell activada.

```powershell
# Instalar el paquete con los extras predeterminados (Edge-TTS + integraciones de plataforma + API HTTP)
pip install -e ".[cloud-tts,integrations,api]"

# Equivalente con uv
uv pip install -e ".[cloud-tts,integrations,api]"

# Opcional: agregar soporte de TTS offline con Piper (sin llamadas a la nube para síntesis de voz)
pip install -e ".[cloud-tts,integrations,api,local-tts]"
```

**Referencia de extras:**

| Extra | Qué habilita |
|---|---|
| `cloud-tts` | Edge-TTS — voz en la nube gratuita de Microsoft (predeterminado) |
| `local-tts` | Piper TTS offline — completamente local, sin llamadas a la nube |
| `integrations` | OBS WebSocket, monitor de VRAM NVIDIA |
| `api` | FastAPI + uvicorn — la API HTTP que usa el front end Tauri para manejar a Kira. Necesaria para correr el producto (`pnpm tauri:debug` la levanta) y para recolectar la suite de tests. |
| `youtube-chat` | Chat de YouTube no oficial (pytchat) — opcional, [leer esto primero](docs/PRIVACY.md#youtube-live-chat-is-opt-in-and-unofficial) |
| `dev` | pytest, pre-commit, detect-secrets |

### Ejecutar

La UI del producto es Tauri, y levanta el backend de Python por su cuenta:

```powershell
cd OpenCohost_UI
pnpm tauri:debug
```

Ver [Requisitos de Tauri](#requisitos-de-tauri) arriba si es tu primera vez — necesita Node.js, pnpm y un toolchain de Rust además de la configuración de Python.

Para un backend suelto (headless, o con el front servido aparte), `run-api.bat` o
`uvicorn opencohost.api.main:app --host 127.0.0.1 --port 8765 --workers 1`.

`python -m opencohost` todavía abre la shell vieja de CustomTkinter, que quedó
**congelada como legacy** el 2026-08-13: se conserva, no se mantiene. Los flags
que arma `EngineHost` no se activan ahí, así que no sirve para validar en runtime.

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
OpenCohost_UI/            # UI del producto — Tauri + React (pnpm tauri:debug)
├── src/features/         # Una carpeta por superficie: agenda, stream, musica, ...
└── src-tauri/backend.rs  # Levanta el backend de Python si no hay ninguno escuchando

opencohost/
├── __main__.py           # Punto de entrada legacy (python -m opencohost)
├── api/                  # Host FastAPI — la superficie del motor del producto
│   ├── engine_host.py    # Composition root: dueño del motor, arma los host flags
│   ├── main.py           # App factory
│   └── routers/          # Un router por superficie (chat, agenda, ptt, obs, ...)
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
├── ui/                   # Shell CustomTkinter LEGACY — congelada, no se mantiene
└── smart_aggregator/     # Agregador de chat en vivo (Twitch; YouTube opcional)
```

## Perfiles de personalidad

En el primer arranque, OpenCohost genera un conjunto de perfiles de personalidad predeterminados en tu `perfiles.json` local (ignorado por git). Cada perfil incluido está etiquetado por idioma, y el conjunto que se genera sigue tu idioma configurado — `opencohost/config/default_profiles.json` incluye ambos:

**Español (`es`)**

| Perfil | Persona |
|---|---|
| Akira | Co-host por defecto — equilibrada y afilada |
| Akira (Learn) | Modo aprendizaje — educativo y alentador |
| Comunidad | Modo comunidad — cálido e inclusivo |
| Calmado | Modo calmado — ritmo más lento, tono sereno |
| Técnico | Modo técnico — preciso, seco, cínico |
| Show | Modo alto impacto — enérgico y performático |

**Inglés (`en`)**

| Perfil | Persona |
|---|---|
| Kira | Co-host por defecto — equilibrada y afilada |
| Kira (Learn) | Modo aprendizaje — educativo y alentador |
| Community | Modo comunidad — cálido e inclusivo |
| Calm | Modo calmado — ritmo más lento, tono sereno |
| Technical | Modo técnico — preciso, seco, cínico |
| Showtime | Modo alto impacto — enérgico y performático |

El nombre Kira y la personalidad base se preservan en todos los perfiles.

## Integración con Ollama

La aplicación valida Ollama al iniciar y desactiva las acciones que dependen del LLM si el servicio no está disponible. El panel de modelos ofrece un único botón contextual:

- **Instalar Ollama** — abre la página oficial de descarga cuando no se encuentra el binario
- **Iniciar Ollama** — ejecuta `ollama serve` cuando está instalado pero no responde
- **Descargar modelo** — descarga el modelo seleccionado cuando Ollama está listo pero el modelo no existe localmente
- **Activar modelo** — cambia al modelo seleccionado cuando ya está instalado
- **Instalar dependencia Python** — avisa cuando falta el paquete `ollama` en el entorno Python activo

El servicio se verifica en `http://127.0.0.1:11434/api/tags`. Si Ollama deja de estar disponible durante una sesión, el motor se marca como no listo y bloquea el procesamiento, el cambio de modelo y las descargas hasta que el servicio vuelva a responder.

## API HTTP

`opencohost/api/` es un proceso FastAPI dueño de su propio motor de Kira —
nunca se comparte con la app Tk legacy, ni esta lo importa. **Es la
superficie del motor del producto:** el front end Tauri en `OpenCohost_UI/`
maneja a Kira enteramente a través de esta API, y `pnpm tauri:debug` la
levanta por ti. Ejecútalo por separado solo si quieres un backend headless o
si estás sirviendo el front end aparte. El extra `api` de
[Configuración](#configuración) ya lo cubre; si instalaste sin él:

```powershell
pip install -e ".[api]"
uvicorn opencohost.api.main:app --host 127.0.0.1 --port 8765 --workers 1
```

**`--workers` DEBE quedar en 1.** Un segundo worker significa un segundo
motor — el doble de carga de VRAM/Ollama y una segunda toma del dispositivo
de audio. `EngineHost` se niega a iniciar una segunda vez en la misma
máquina mediante un lockfile.

**No hagas bind a `--host 0.0.0.0`.** Eso expone la superficie de control
del motor a tu LAN. CORS solo restringe a quien llama desde un navegador —
no hace nada contra un script o `curl` que golpee el puerto directamente.
Mantén esto en loopback salvo que pongas un proxy con autenticación
delante.

Endpoints: `GET /api/status` (snapshot de salud/motor, solo lectura) y
`POST /api/perfiles/switch` (cambia el perfil de personalidad activo,
idempotente vía el header `Idempotency-Key`).

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
