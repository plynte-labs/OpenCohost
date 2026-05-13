# Arquitectura de VoiceAI

Aplicación de asistente de voz IA ("Kira") para streamers, con procesamiento 100% local-first.

## Estructura de Directorios (Real)

```
VoiceAI/
├── main.py                     # Entry point — init theme, crea VocalAIApp, arranca mainloop
├── server_qwen.py              # Servidor HTTP local para Qwen3-TTS (modo pesado)
├── mudanza.py                  # Script de migración (legacy)
│
├── config/
│   ├── __init__.py
│   ├── settings.py             # Constantes globales, rutas, catálogo de modelos, timeouts
│   ├── storage.py              # Resolver portable de cache/temp/Ollama (TEMP, HF, Torch)
│   ├── storage.yaml            # Config editable de disco/cache por usuario
│   ├── logger.py               # Logger estructurado (consola + archivo en logs/)
│   ├── ptt_settings.json       # Configuración persistente de PTT (hotkey, estado)
│   ├── window_geometry.json    # Posición/tamaño de ventana (persistencia multi-monitor)
│   ├── smart_aggregator.yaml   # Config RF3: filtros, thresholds, vibe window
│   └── stream_admin.yaml       # Config RF4: OAuth, moderación, presets
│
├── core/
│   ├── __init__.py
│   ├── llm_engine.py           # MotorVocalIA — hilo IA: Ollama, TTS, memoria conversacional
│   └── profiles.py             # Carga/guardado de perfiles (personalidades/prompts)
│
├── ui/
│   ├── __init__.py
│   ├── app.py                  # VocalAIApp — ventana principal, delega a app_shell
│   ├── app_shell.py            # AppShell — layout principal, coordina paneles y motor
│   ├── protocols.py            # Interfaces/contratos entre componentes UI
│   ├── state.py                # UIState — estado reactivo centralizado con dispatcher
│   │
│   ├── voice_control.py        # Panel de grabación: PTT, InputStream, anti-loop filter
│   ├── ptt_manager.py          # PTTManager — global hotkey con pynput (keyboard + mouse)
│   ├── model_panel.py          # Panel de modelos: selección, descarga, info de Ollama
│   ├── profile_panel.py        # Panel de perfiles: selección y edición de personalidades
│   ├── profiles_window.py      # Ventana modal para CRUD de perfiles
│   ├── advanced_panel.py       # Panel avanzado (debug, config interna)
│   ├── status_bar.py           # Barra de estado inferior
│   │
│   ├── smart_aggregator_ui.py  # UI del Smart Aggregator (RF3) — pestaña YT Chat
│   └── stream_admin_ui.py      # UI de Stream Admin (RF4) — pestaña Stream Admin
│
├── smart_aggregator/           # RF3 — Procesador inteligente de chat
│   ├── __init__.py
│   ├── aggregator.py           # Orquestador headless del aggregator
│   ├── message_filter.py       # Filtro: longitud, emotes, menciones, repetitivo, gibberish, ASCII art, quality score
│   ├── intent_aggregator.py    # Agrupa chat por intención + entidades dinámicas (sin nombres hardcodeados)
│   ├── vibe_thermometer.py     # Análisis de sentimiento del chat (1 LLM call)
│   ├── activity_trigger.py     # Detector de picos de actividad (msgs/sec)
│   ├── session_history.py      # Persistencia: SQLite + JSONL por sesión
│   ├── chat_source.py          # Fuente de chat: YouTube Live (pytchat)
│   └── ...                     # Tests, adapters, interfaces
│
├── stream_admin/               # RF4 — Gestión de stream (pausado en lectura)
│   ├── __init__.py
│   ├── admin_manager.py        # Orquestador headless del admin
│   ├── providers.py            # Interfaz de proveedor (YouTube/Twitch)
│   ├── youtube_provider.py     # Implementación YouTube Data API v3 + OAuth
│   ├── moderation.py           # Motor de moderación: timeout, ban, slow mode
│   ├── analytics.py            # Tracker de analíticas: viewers, subs, chat velocity
│   └── ...                     # OAuth store, tests
│
├── docs/                       # Documentación del proyecto
│   ├── architecture.md         # Este documento
│   ├── DECISIONS.md            # Architecture Decision Records (el por qué)
│   ├── TROUBLESHOOTING.md      # Bugs conocidos y cómo se resolvieron
│   ├── changes.md              # Changelog técnico con notas de implementación
│   ├── UI_ARCHITECTURE.md      # Detalle de la refactorización UI
│   └── HANDOFF_RF*.md          # Handoffs por feature (RF1-RF4)
│
├── temp/                       # Audio temporal (chunks TTS, grabaciones)
├── logs/                       # Logs de la aplicación
├── Grabaciones/                # Grabaciones de voz del streamer (referencia TTS)
├── modelos_f5/hub/             # Modelos Qwen3-TTS cacheados (offline)
├── data/                       # Datos persistentes (OAuth tokens, sesiones)
├── perfiles.json               # Perfiles de personalidad de Kira
│
├── tests/                      # Tests unitarios e integración
├── conductor/                  # SDD/Conductor tracks y specs
└── legacy/                     # Código legacy (monolito original)
```

## Flujo de Información

1. **Entry Point**: `python main.py` → configura tema customtkinter → crea `VocalAIApp` → `app.protocol("WM_DELETE_WINDOW", on_closing)` → `app.mainloop()`.

2. **Inicialización diferida**: `AppShell.__init__()` construye la UI pero **NO** arranca el motor IA directamente. Usa `self.after(100, self._start_motor)` para garantizar que `mainloop()` esté activo antes de que el thread invoque callbacks UI.

3. **Configuración**: Todos los componentes leen desde `config/settings.py` (constantes) y archivos YAML/JSON (config persistente).

4. **Motor IA**: `MotorVocalIA` corre en hilo daemon. Comunicación asíncrona vía `command_queue` (UI → motor) y `ui_callback` (motor → UI). Gestiona Ollama (LLM), memoria conversacional, y pipeline TTS (edge-tts o Qwen3-TTS local).

5. **UI**: `AppShell` coordina paneles delegados. `UIState` gestiona estado reactivo. Cada panel tiene responsabilidad única (modelos, perfiles, voz, admin, etc.).

## Threading Model

VoiceAI usa múltiples hilos concurrentes. Este es el mapa completo:

| Hilo | Origen | Rol | Thread-safe | Notas |
|------|--------|-----|-------------|-------|
| **Main Thread** | `main.py` | Tkinter mainloop, eventos UI | N/A (single-threaded UI) | Único thread que puede modificar widgets |
| **MotorVocalIA** | `app_shell.py` → `_start_motor()` | LLM inference, TTS pipeline, gestión de memoria | `command_queue` (thread-safe), `_lock` para flags | Daemon. Callbacks UI vía `self.after(0, ...)` |
| **Productor TTS** | `llm_engine.py` → `_hablar()` | Genera chunks de audio (edge-tts o HTTP Qwen3) | `cola_audios` (Queue, thread-safe) | Daemon. Corre dentro del hilo MotorVocalIA |
| **Download Worker** | `llm_engine.py` → `download_model` | Descarga modelos de Ollama con progreso | `log_queue` (thread-safe) | Daemon. Thread separado dentro del motor |
| **PTT Keyboard Listener** | `ptt_manager.py` | Detecta key press/release global | Callbacks vía `after()` | Daemon. pynput.keyboard.Listener |
| **PTT Mouse Listener** | `ptt_manager.py` | Detecta mouse press/release global | Callbacks vía `after()` | Daemon. pynput.mouse.Listener |
| **Audio InputStream** | `voice_control.py` | Captura audio del micrófono (sounddevice) | Buffer en memoria, valida en release | Callback al main thread via `after()` |
| **Log Dispatcher** | `state.py` | Procesa cola de logs y actualiza UI | `_dispatch_thread` | Usa `after()` para actualizar widgets |
| **YouTube Chat Source** | `chat_source.py` | Recibe mensajes de YouTube Live (pytchat) | Callbacks thread-safe | `interruptable=False` para evitar signal error |
| **Aggregator Worker** | `smart_aggregator_ui.py` | Procesa mensajes agregados del chat | Thread dedicado | Callbacks via `after()` |
| **Stream Admin Worker** | `stream_admin_ui.py` | OAuth refresh, analíticas periódicas | Thread dedicado | Callbacks via `after()` |

### Regla de Oro: Main Thread Only para UI

**Ningún hilo secundario puede modificar widgets directamente.** Todos los callbacks de hilos hacia la UI deben usar `self.after(0, callback)` para encolar la operación en el main loop.

Excepción: `self.after()` solo funciona si `mainloop()` está activo. Por eso el motor IA se inicia diferido (`self.after(100, ...)`).

### Contrato de Comunicación

```
UI ──command_queue──> MotorVocalIA  (thread-safe Queue)
MotorVocalIA ──ui_callback──> UI   (self.after(0, handler))

UI ──callbacks──> PTTManager       (pynput listeners → after())
PTTManager ──on_release──> UI     (valida → envía a command_queue)

UI ──llm_interface──> Aggregator   (inyectado, reutiliza motor)
Aggregator ──callbacks──> UI      (after())

PTT ──buffer + grace period──> voice_control.py ──flush──> MotorVocalIA
YouTube Chat ──enqueue──> Priority Queue ──pop──> MotorVocalIA
Overflow ──enqueue_accumulation──> Accumulation Buffer ──compact──> MotorVocalIA
```

### Flujo de PTT con Buffer y Cola Prioritaria

```
┌──────────────────────────────────────────────────────────┐
│  PTT (F8 presionada)                                     │
│    → Limpia buffer anterior                              │
│    → Acumula transcripciones de LiveAudio (máx 500 chars)│
│    → Al soltar: grace period 2s (delay STT)              │
│    → Flush watcher envía buffer al motor                 │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│  Cola Prioritaria (máx 5 items)                          │
│    Prioridad 0: PTT (streamer hablando)                  │
│    Prioridad 1: YouTube chat (mensajes agregados)        │
│    Overflow → Buffer de Acumulación                      │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│  Buffer de Acumulación (máx 50 items, 2000 chars, 2 min) │
│    Guarda: overflow de cola + mensajes mientras ocupado  │
│    Al motor libre: compacta todo en 1 consulta           │
│    Limpia buffer después de enviar                       │
└──────────────────────────────────────────────────────────┘

MotorVocalIA procesa SECUENCIALMENTE (nunca 2 a la vez):
  1. Toma siguiente de cola prioritaria (PTT primero)
  2. Ejecuta inferencia Ollama
  3. Sintetiza TTS
  4. Si hay acumulación → compacta y envía
  5. Vuelve a esperar
```

## Reglas de Threading y Ciclo de Vida

### Motor IA — arranque diferido

El hilo `MotorVocalIA` **NO** debe arrancarse directamente en `__init__`. Se difiere con
`self.after(100, self._start_motor)` para garantizar que `mainloop()` ya esté activo antes
de que el thread invoque callbacks UI (`self.after(0, ...)`).

**Sin el defer:** race condition → `RuntimeError: main thread is not in main loop` cuando
`_check_ollama_service()` termina antes de que `mainloop()` arranque y llama `ui_callback("ready")`.

### Regla general

Cualquier hilo que invoque callbacks hacia Tkinter (vía `self.after()`) solo debe iniciarse
**después** de que `mainloop()` esté corriendo. Si el hilo necesita arrancar durante la
construcción de la ventana, usar `self.after(delay_ms, thread.start)` para diferirlo.

## Agregar Nuevas Funciones

- **Para agregar una nueva configuración o un nuevo modelo de Ollama**, modifique `config/settings.py`.
- **Para actualizar la forma en la que la IA responde o maneja memoria**, edite `core/llm_engine.py`.
- **Para cambiar el comportamiento visual, agregar botones o ventanas**, busque en `ui/app_shell.py` o cree un nuevo archivo `.py` en la carpeta `ui/`.
- **Para lógica de negocio o de backend adicional**, considere agregar un archivo en la carpeta `core/` y enlácelo con la UI a través de `app_shell.py`.
- **Para nuevo módulo independiente**, crear carpeta en raíz con su propio `__init__.py` y conectar vía contratos/callbacks (ver RF3 smart_aggregator como referencia).
