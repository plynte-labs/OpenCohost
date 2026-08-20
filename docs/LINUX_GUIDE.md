# OpenCohost — Guía de Ejecución y Empaquetado en Linux (Pop!_OS / Ubuntu)

Esta guía detalla cómo configurar, ejecutar y empaquetar **OpenCohost** en Linux (especialmente Pop!_OS y distros basadas en Debian/Ubuntu), utilizando proveedores Cloud (NVIDIA NIM / OpenAI-compatible) y síntesis de voz ligera (Edge-TTS / Piper) para no depender de GPUs dedicadas ni modelos pesados en local.

---

## 1. Arquitectura y Compatibilidad Multiplataforma

OpenCohost está estructurado para operar de forma desacoplada:
- **Backend Core (`opencohost/`)**: Servicio FastAPI en Python (`opencohost.api.main:app`) que expone el motor de IA, memoria, perfiles y orquestación de voz.
  - Las llamadas a APIs específicas de Windows (`ctypes.windll`, `user32.GetAsyncKeyState`) operan bajo un patrón *fail-open* (`sys.platform != "win32"`), garantizando que no se interrumpa el flujo en Linux.
  - El sistema de archivos almacena configuraciones y caché en `~/.config/OpenCohost/` y en la raíz del proyecto.
  - El audio opera de forma nativa a través de **PipeWire / PulseAudio / ALSA** mediante `sounddevice` y `portaudio`.
- **Frontend UI (`OpenCohost_UI/`)**: Shell de escritorio construido en **Tauri v2 + React/Vite + Tailwind**.
  - En Linux genera ejecutables nativos y paquetes de distribución (`.deb` y `.AppImage`).
  - Lanza automáticamente el backend de Python en segundo plano (`backend.rs`).

---

## 2. Requisitos Previos del Sistema en Pop!_OS

Instalar las librerías nativas requeridas para compilación de Tauri, WebKit2GTK y PortAudio:

```bash
sudo apt update && sudo apt install -y \
  libwebkit2gtk-4.1-dev \
  build-essential \
  curl \
  wget \
  file \
  libssl-dev \
  libgtk-3-dev \
  libayatana-appindicator3-dev \
  librsvg2-dev \
  portaudio19-dev \
  libasound2-dev
```

Asegurate de contar con:
- **Python 3.10+** (probado y verificado en 3.13)
- **Node.js 20+** y **pnpm** (`npm install -g pnpm`)
- **Rust toolchain** (`curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`)

---

## 3. Configuración del Entorno Python

1. Crear y activar el entorno virtual en la raíz del repositorio:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. Instalar el paquete en modo editable con los extras de API, Cloud TTS y desarrollo:
   ```bash
   pip install --upgrade pip
   pip install -e ".[api,cloud-tts,local-tts,dev]"
   ```

3. Verificar que los tests unitarios pasen:
   ```bash
   pytest tests/test_llm_tiers.py tests/test_model_panel.py tests/test_health_monitor.py -q
   ```

---

## 4. Configuración de LLM en la Nube (NVIDIA NIM / OpenAI-compatible)

Para utilizar OpenCohost sin requerir Ollama local ni agotar la VRAM de tu laptop:

1. Copiar la plantilla de variables de entorno:
   ```bash
   cp .env.example .env
   ```

2. Configurar tu API key de Nvidia en `.env`:
   ```env
   OPENCOHOST_LLM_PROVIDER=cloud
   NVIDIA_API_KEY=nvapi-TU_API_KEY_AQUI
   NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1
   NVIDIA_MODEL=meta/llama-3.1-70b-instruct
   OPENCOHOST_TTS_ENGINE=edge-tts
   EDGE_TTS_VOICE=es-AR-TomasNeural
   ```

3. **Alternativa en runtime (`config/llm_provider.json`)**:
   El motor soporta perfiles dinámicos mediante `config/llm_provider.json` y `config/llm_keys.json`, permitiendo alternar entre proveedores cloud y local desde la UI.

4. **Modelos para Síntesis de Voz Offline (Piper TTS)**:
   Si querés usar síntesis de voz 100% offline (sin Edge-TTS), descargá las voces ONNX en `modelos_f5/piper/`:
   ```bash
   mkdir -p modelos_f5/piper
   # Voz Argentina (predeterminada)
   curl -L "https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_AR/daniela/high/es_AR-daniela-high.onnx" -o modelos_f5/piper/es_AR-daniela-high.onnx
   curl -L "https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_AR/daniela/high/es_AR-daniela-high.onnx.json" -o modelos_f5/piper/es_AR-daniela-high.onnx.json
   ```

---

## 5. Ejecución en Desarrollo

### Opción A: Modo Integrado (Frontend Tauri + Backend Automático)
```bash
cd OpenCohost_UI
pnpm install
pnpm tauri dev
```
*Tauri detectará la configuración en `src-tauri/backend.config.json` y levantará el backend de Python automáticamente.*

### Opción B: Backend y Frontend por Separado
1. **Terminal 1 (Backend FastAPI)**:
   ```bash
   source .venv/bin/activate
   uvicorn opencohost.api.main:app --host 127.0.0.1 --port 8765 --workers 1
   ```
2. **Terminal 2 (Frontend React en navegador o Vite)**:
   ```bash
   cd OpenCohost_UI
   pnpm dev
   ```

---

## 6. Generación del Instalador y Paquetes para Linux (`.deb` y `.AppImage`)

Para compilar la aplicación de producción empaquetada:

```bash
cd OpenCohost_UI
pnpm install
pnpm tauri build
```

Los paquetes resultantes se generarán en:
- **Debian / Ubuntu / Pop!_OS package**:
  `OpenCohost_UI/src-tauri/target/release/bundle/deb/opencohost_*.deb`
- **Universal Linux AppImage**:
  `OpenCohost_UI/src-tauri/target/release/bundle/appimage/opencohost_*.AppImage`

Para instalar el `.deb` en tu sistema:
```bash
sudo dpkg -i OpenCohost_UI/src-tauri/target/release/bundle/deb/opencohost_*.deb
```

---

## 7. Consideraciones Técnicas de Linux

- **Push-to-Talk (PTT) y Atajos Globales**: En sesiones X11, `pynput` captura hotkeys de fondo de forma transparente. En sesiones Wayland puras, la captura de teclado de fondo puede estar restringida por el compositor (GNOME/COSMIC) a menos que se ejecute sobre XWayland o se otorguen permisos de lectura en `/dev/input/`.
- **Compatibilidad con Windows**: Ningún cambio introducido para Linux afecta el soporte de Windows. Los bloques de código nativo de Windows permanecen intactos bajo condicionales `sys.platform == "win32"` y `#[cfg(windows)]` en Rust.
