Este es el **Documento de Especificación de Requerimientos y Arquitectura Sugerida (v1.0)** para el proyecto **VoiceAI - Kira**. Este documento define la transición de un sistema reactivo de bucle abierto a uno de control determinista gestionado por el usuario, optimizado para entornos de alto rendimiento (Streaming/Gaming).

---

**[WorkerSeniorAI] Hardening seguridad/privacidad parcial — 2026-05-05:** En rama `feature/security-privacy-hardening` se aplicaron correcciones defensivas sin migrar aun OAuth/Client: `read_only` bloquea escritura en backend, la UI deshabilita controles de escritura cuando no hay modo/scope write, logs visuales tienen limite de lineas, redaccion de secretos fue reforzada, `VOICEAI_DEBUG=1` controla debug, RF3 ejecuta cleanup al iniciar/cerrar, chat autenticado corta con backoff tras fallos y los eventos demo de Kira Acciones ya no se persisten como auditoria real. Pendiente importante: migrar Tokens OAuth y OAuth Client fuera de JSON plano a Credential Manager/DPAPI/keyring.

**[OBS WebSocket Avatar Integration] — 2026-05-12:** En rama `feature/kira-product-ui-redesign`:
- **avatar/obs_client.py**: Cliente OBS WebSocket que se suscribe a `AvatarStateBridge` y actualiza fuentes de imagen en OBS vía `set_input_settings`. Cambio de estado del avatar (idle/listening/thinking/speaking/sleeping) → cambio automático de imagen en escena OBS.
- **avatar/avatar_config.py**: Agregada dataclass `OBSConfig` (host, port, password, source_name, scene_name).
- **config/avatar.yaml**: Nueva sección `obs:` con configuración de conexión.
- **ui/avatar_panel.py**: Panel de configuración OBS con toggle habilitar/deshabilitar, campos host/puerto/password/fuente, botón "Probar conexión", estado de conexión.
- **ui/app_shell.py**: Inicialización de `OBSClient` al arrancar, desconexión al cerrar.
- **tests/test_obs_client.py**: 9 tests del cliente OBS.
- **Clave técnica**: OBS WebSocket v5 requiere setear AMBOS campos `file` y `local_file` con `overlay=False` para que la fuente de imagen actualice y renderice correctamente.

## 📄 Especificación de Requerimientos del Sistema (SRS)

### 1. Módulo de Control de Entrada (Control & PTT)
**Propósito:** Sustituir la escucha continua por un sistema de captura bajo demanda para eliminar falsos positivos y reducir carga computacional.

*   **RF1.1 - Gestión de Feature Toggle:** La UI debe incluir un interruptor (Switch) maestro para activar/desactivar la función de **Push-to-Talk (PTT)**. ✅ *Implementado en rama `feature/rf1-ptt-hotkey`.*
    *   **Implementación técnica:**
        *   Se agregará un `CTkSwitch` en `ui/app.py` (frame de control) vinculado a `self.ptt_enabled`.
        *   Cuando PTT está **OFF**, el sistema opera en modo WebSocket continuo (comportamiento actual).
        *   Cuando PTT está **ON**, el WebSocket se pausa y la captura de audio solo ocurre bajo demanda del hotkey.
        *   Estado persistido en memoria de la app (no requiere disco).

*   **RF1.2 - Remapeo de Teclas:** Implementación de un selector de entrada en la interfaz para configurar el **Global Hotkey** (ej. F10, Mouse4) mediante la librería `pynput`. ✅ *Implementado en rama `feature/rf1-ptt-hotkey`.*
    *   **Implementación técnica:**
        *   Dependencia: `pip install pynput`.
        *   Hilo daemon `pynput.keyboard.Listener` + `pynput.mouse.Listener` iniciado en `ui/app.py`.
        *   Dropdown en UI con teclas predefinidas: `F1`–`F12`, `Mouse4`, `Mouse5`, `ScrollLock`, `Insert`.
        *   Eventos `on_press` / `on_release`: inician y detienen la grabación de audio (float32, 24000Hz).
        *   Al soltar la tecla, el buffer de audio se valida (RMS mínimo, duración) y se envía al motor IA como comando `process_context`.
        *   El listener corre en un hilo independiente del mixer de pygame para evitar latencia.
*   **RF1.3 - Lógica de Captura Half-Duplex:** El sistema debe ignorar cualquier entrada de audio mientras el motor TTS esté activo (`self._speaking == True`). El procesamiento del buffer capturado solo iniciará una vez que Kira haya terminado de reproducir su audio actual.
*   **RF1.4 - Integración Silero VAD:** El PTT actuará como una compuerta lógica sobre el flujo de WebSocket existente. Aunque el PTT esté presionado, solo se enviará el segmento de audio a Whisper si Silero detecta voz humana de alta confianza.

### 2. Interfaz y Experiencia de Usuario (UI/UX)
**Propósito:** Minimizar el impacto en los recursos del sistema y evitar interferencias visuales en el monitor principal.

*   **RF2.1 - Arquitectura Multi-Monitor:** El diseño de la interfaz se optimizará para escalado en un segundo monitor, eliminando la necesidad de capas de **Overlay** (DirectX/Vulkan) que consumen ciclos de GPU críticos durante el juego. ✅ *Implementado — persistencia de geometría.*
*   **RF2.2 - Estado del Pipeline:** La UI debe mostrar indicadores visuales del estado del "grifo" (Tap State): *Escuchando*, *Procesando LLM*, *Sintetizando Voz* y *En Espera*. ✅ *Implementado — texto + colores + barra RMS.*
*   **RF2.3 - Registro de Acciones (Console Log):** Inclusión de un panel de actividad donde Kira confirme las acciones administrativas ejecutadas (ej: "Título de Twitch actualizado con éxito"). ✅ *Implementado — pestaña Kira Acciones + persistencia JSONL.*

### 3. Procesador Inteligente de Chat (Smart Aggregator)
**Propósito:** Escalar la interacción para audiencias masivas mediante algoritmos de consolidación. Modular, en `smart_aggregator/` independiente del core.

*   **RF3.1 - Filtro de Longitud y Calidad:** Descarte automático de mensajes cortos, emojis puros, enlaces y menciones. Umbrales configurables. Whitelist de usuarios VIP/Mod. ✅ *Implementado en `smart_aggregator/message_filter.py`.*
*   **RF3.2 - Vibe Thermometer:** Ventana de tiempo configurable (default 120s) con una sola inferencia LLM al final. Reutiliza el motor LLM existente mediante `llm_interface` inyectada y no llama Ollama directamente. Analiza: excitement, sadness, anger, joy, confusion, neutral. Retorna temperatura global. ✅ *Implementado en `smart_aggregator/vibe_thermometer.py`.*
*   **RF3.3 - Trigger por Actividad:** Medición de mensajes/segundo en ventana deslizante usando timestamps del mensaje. Umbral configurable. Acciones: auto_reply (mensaje predefinido) y/o behavior_change (parámetro de excitación). ✅ *Implementado en `smart_aggregator/activity_trigger.py`.*
*   **RF3.4 - Historial de Chat (Híbrido):** Persistencia por sesiones. SQLite para búsquedas rápidas, JSONL para audit trail. Retención configurable (default 7 días). ✅ *Implementado en `smart_aggregator/session_history.py`.*
*   **RF3.5 - Chat Source (YouTube):** Fuente de chat via `pytchat`/`chatdownload`. Video ID configurable. Handle reconnects. ✅ *Implementado en `smart_aggregator/chat_source.py` (requiere instalar `pytchat`).*

**[WorkerSeniorAI] Correcciones RF3 — 2026-05-04:** Se corrigió el contrato LLM para evitar cargas directas de Ollama, el cálculo de actividad por ventana, la limpieza JSONL por `session_id`, el flujo de reconexión de `YouTubeChatSource`, la orquestación headless de `Aggregator` y los tests locales TC3.1-TC3.6. Verificación realizada con el Python obligatorio `E:\Miniconda\envs\flux_env\python.exe`.

**[WorkerSeniorAI] Integración RF3 UI — 2026-05-04:** Con autorización explícita del usuario, `ui/app.py` ahora instancia `Aggregator`, permite pegar URL/`video_id` de YouTube Live, usa un adapter `llm_interface` silencioso contra el modelo activo de `MotorVocalIA`, bloquea análisis cuando Kira está ocupada y envía a Kira solo contexto agregado ante picos de actividad. No se modificó `core/llm_engine.py`.

**[WorkerSeniorAI] Manejo de errores YouTube — 2026-05-04:** La prueba con `video_id=-MtbPcNE8ls` recibió `429`/timeout SSL desde YouTube. Se agregaron callbacks de error/conexión/desconexión de fuente para que la UI registre el fallo y recupere el estado del botón si `pytchat` no logra conectar tras retries.

**[WorkerSeniorAI] Fix `pytchat` thread UI — 2026-05-04:** Se corrigió el error `signal only works in main thread` configurando `pytchat.create(..., interruptable=False)` desde `YouTubeChatSource`, con `source.interruptable: false` en YAML.

**[WorkerSeniorAI] Filtro emojis personalizados — 2026-05-04:** `MessageFilter` ahora reconoce aliases de emojis de YouTube tipo `:bird:`. Los mensajes solo con aliases se descartan y los aliases mezclados con texto se limpian antes de pasar callbacks/logs. También se reforzó la regex de menciones para handles con guiones/puntos.

**[WorkerSeniorAI] Tuning live real — 2026-05-04:** Para que RF3 dispare en chats moderados, `activity.threshold_per_second` se bajó a `1.0`, `cooldown_seconds` subió a `45.0`, `vibe.window_seconds` bajó a `60` y el prompt de vibe se ajustó a chat bilingüe con JSON estricto. La UI ahora indica notas de fallback del vibe.

**[WorkerSeniorAI] UI YT Chat + anti-spam — 2026-05-04:** Se agregó pestaña `YT Chat`, el Log General ya no recibe cada mensaje de YouTube, RF3 deduplica mensajes repetidos por usuario, aplica rate-limit configurable por usuario (`Max/u` en UI) y el contexto enviado a Kira ahora pide resumen del flujo con un solo mensaje destacado.

**[WorkerSeniorAI] Logs operativos RF3 — 2026-05-04:** Se limpiaron duplicados de conexión/desconexión, errores transitorios de YouTube ahora aparecen como avisos de reconexión y los fallbacks de vibe se muestran en lenguaje operativo en vez de códigos internos crípticos.

**[WorkerSeniorAI] Respuesta natural RF3 — 2026-05-04:** Se ajustó el prompt de picos para que Kira reaccione directo como co-host y no describa el análisis con frases técnicas como “energía del flujo” o “mensaje destacado”.

**[WorkerSeniorAI] Cierre funcional RF3 — 2026-05-04:** RF3 queda cerrado satisfactoriamente como Smart Aggregator funcional integrado a VoiceAI. El detalle de funcionalidades, comportamiento esperado, casos cubiertos, configuración operativa y próximos pasos quedó documentado en `docs/RF3_Smart_Aggregator_Spec.md`, sección `[WorkerSeniorAI] Cierre Funcional RF3`.

---

## 📋 Resumen de Implementación

| RF | Nombre | Estado |
|---|---|---|
| RF1.1 | Toggle PTT | ✅ |
| RF1.2 | Hotkey Remap | ✅ |
| RF1.3 | Half-Duplex | ✅ |
| RF1.4 | Silero VAD Gate | ⏳ Pendiente |
| RF2.1 | Multi-Monitor UI | ✅ |
| RF2.2 | Pipeline Visual | ✅ |
| RF2.3 | Panel Kira Acciones | ✅ |
| RF3.1 | Filtro Longitud/Calidad | ✅ |
| RF3.2 | Vibe Thermometer (1 LLM) | ✅ |
| RF3.3 | Trigger por Actividad | ✅ |
| RF3.4 | Historial Chat Híbrido | ✅ |
| RF3.5 | Chat Source YouTube | ✅ |

---

## ❓ Preguntas para RF3 — Respuestas del Usuario

| # | Pregunta | Respuesta |
|---|---|---|
| Q1 | Fuente de chat | YouTube (de momento). Selector de proveedor (futuro). |
| Q2 | Filtro de calidad | Umbral configurable. Descartar: enlaces, menciones, emojis puros. Whitelist VIP/Mod. Automático. |
| Q3 | Vibe Thermometer | **CRÍTICO**: Un solo modelo LLM en ejecución. NO crear segundo modelo. Ventana configurable. Todas las emociones. Afecta comportamiento internamente. |
| Q4 | Trigger por Actividad | Responder + cambiar comportamiento. Umbral por definir (no especificado aún). |
| Q5 | Historial | **Híbrido**: SQLite (búsquedas) + JSONL (audit). Por sesiones. Retención configurable. |

**Restricción arquitectónica #1:** Todo debe ser **modular**. Módulo `smart_aggregator/` independiente del core (`ui/app.py`, `motor_ia.py`).

**Restricción arquitectónica #2:** **Un solo LLM corriendo**. Si el core ya tiene Ollama activo, el aggregator lo reutiliza. No instancia nuevo.

**Restricción arquitectónica #3:** **Configuración via YAML**. Sin valores hardcoded.

**Restricción arquitectónica #4:** **Callbacks para comunicar con el core**. El aggregator NO modifica el código existente, solo emite eventos.

---

## 📋 Especificación para Agente IA

Ver documento detallado: **`docs/RF3_Smart_Aggregator_Spec.md`**

Este documento incluye:
- Arquitectura modular (`smart_aggregator/` con 7 archivos)
- Contrato de eventos (callbacks con el core)
- Qué HACER y qué NO HACER para cada RF
- Interfaz de cada clase
- Configuración YAML completa
- Dependencias permitidas vs prohibidas
- Criterios de aceptación
- Orden de implementación sugerido

### 4. Gestión de Stream (Admin Mode)
**Propósito:** Automatizar tareas de producción mediante integración segura con APIs oficiales de plataformas de streaming, empezando por YouTube y dejando Twitch preparado como proveedor futuro.

**Estado:** Diseño refinado. Pendiente implementación. Primero debe implementarse modo **solo lectura** para validar OAuth 2.0 seguro antes de permitir acciones de escritura.

*   **RF4.1 - Integración YouTube/Twitch API para Metadata:** Módulo para leer y modificar título, categoría, descripción y tags del stream. YouTube primero; Twitch futuro mediante interfaz de proveedor. Los cambios sugeridos por Kira requieren aprobación del streamer por defecto, salvo configuración explícita de auto-aprobación. El streamer puede editar sugerencias antes de aplicarlas. Incluye presets de títulos y categorías predeterminadas por el streamer.
*   **RF4.2 - Moderación Automática y Asistida:** Capacidad configurable para operar en tres modos: `Solo alertas`, `Confirmación requerida` y `Automático`. La detección de toxicidad masiva registra primero un evento en logs de Kira. Slow Mode puede activarse/desactivarse manual o automáticamente si el streamer lo permite. Acciones como Emote-Only, Followers-Only, Subscribers-Only, timeout o ban dependen de soporte del proveedor y deben priorizar confirmación para acciones de alto riesgo. Kira puede anunciar acciones de moderación al chat solo si está configurado.
*   **RF4.3 - Reporte de Analíticas:** Acceso configurable a viewers concurrentes, eventos de subs/follows/donaciones/bits, chat velocity, vibe trend y stream uptime. Las analíticas se muestran en UI y se inyectan como contexto resumido al LLM con frecuencia configurable. Kira puede reaccionar a hitos/eventos importantes, mientras que tendencias generales se usan como contexto silencioso.
*   **RF4.4 - UI Stream Admin:** Nueva pestaña `Stream Admin` para estado OAuth, metadata, propuestas pendientes, presets, moderación, analíticas y acciones administrativas.

**Documentos RF4:**
- `docs/RF4_Functional_Requirements.md`
- `docs/RF4_Quality_Requirements.md`
- `docs/RF4_Test_Scenarios.md`
- `docs/HANDOFF_RF4.md`

**[WorkerSeniorAI] Refinamiento RF4 — 2026-05-04:** Se registraron las decisiones del usuario para RF4: YouTube primero, Twitch futuro, OAuth seguro con etapa inicial solo lectura, cambios de metadata con aprobación por defecto, presets, categorías sugeridas por Kira, tres modos de moderación, analíticas configurables desde UI y nueva pestaña `Stream Admin`. Se recomienda arquitectura `stream_admin/` separada, dependiente por contrato de RF3 para consumir vibe/chat velocity, sin modificar el core ni cargar otro LLM.

**[WorkerSeniorAI] Cierre de dudas RF4 MVP — 2026-05-04:** Se cerraron decisiones MVP: tokens OAuth en archivo local ignorado por git como opción simple inicial, YouTube read-only real con placeholder Twitch, Kira puede escribir mensajes al chat si se habilita, `timeout`/`ban` entran en MVP con confirmación por defecto y presets en `config/stream_admin.yaml`.

**[WorkerSeniorAI] Implementación RF4 MVP base — 2026-05-04:** Se creó `stream_admin/` con `AdminManager`, proveedor YouTube OAuth/API usando `requests`, placeholder Twitch, token store local, motor de moderación, tracker de analíticas, tests headless y `config/stream_admin.yaml`. `ui/app.py` integra una pestaña `Stream Admin` con OAuth YouTube lectura/escritura, metadata editable, sugerencias de Kira, acciones pendientes, moderación, mensajes al chat y analíticas RF3 como contexto silencioso. Pendiente prueba end-to-end con credenciales OAuth reales del usuario.

**[WorkerSeniorAI] OAuth RF4 desde UI — 2026-05-04:** Se agregaron campos `Client ID` y `Secret` en `Stream Admin` para guardar credenciales OAuth YouTube localmente en `data/stream_admin/oauth_client.json`, archivo ignorado por git. Esto evita editar YAML o variables de entorno para el flujo MVP.

**[WorkerSeniorAI] Operación RF4 para streams chicos — 2026-05-05:** Se agregaron controles `Stream Chico`, `Simular Chat` y `Forzar Kira` en `Stream Admin`. `Stream Chico` baja umbral/cooldown de RF3 en runtime para canales con poca audiencia, `Simular Chat` inyecta mensajes de prueba al agregador y `Forzar Kira` hace que Kira comente con contexto reciente aunque no haya pico automático.

**[WorkerSeniorAI] Moderación por usuario RF4 — 2026-05-05:** `Stream Admin` ahora lista usuarios recientes del chat autenticado con `channelId`, contador de mensajes, campo de razón editable y botones `Timeout` / `Banear`. Las acciones piden confirmación antes de ejecutar y usan la API de YouTube con permisos de escritura; el owner aparece con acciones deshabilitadas.

**[WorkerSeniorAI] Scroll global RF4 — 2026-05-05:** La pestaña `Stream Admin` ahora usa un `CTkScrollableFrame` global para que metadata, moderación, usuarios recientes, controles de Kira y logs sean accesibles en pantallas pequeñas o modo compacto.

**[WorkerSeniorAI] Cierre funcional RF4 MVP — 2026-05-05:** RF4 queda funcional como MVP de Stream Admin para YouTube: OAuth desde UI, lectura de live privado/no listado, metadata editable, escritura con scope `youtube.force-ssl`, chat autenticado por `liveChatId`, envío de mensajes al chat, integración RF3/RF4, modo `Stream Chico`, simulación de chat, botón `Forzar Kira`, lista de usuarios recientes y acciones `Timeout`/`Banear` con confirmación. Twitch queda como placeholder futuro y Slow Mode/Emote-only dependen de soporte/API futura.

**[WorkerSeniorAI] Refactor UI/UX seguro — 2026-05-05:** Se reorganizó `ui/app.py` sin tocar backend ni contratos: la vista `Kira` quedó como experiencia principal con respuesta central, botón grande `Hablar`/`Detener`, estados de voz/PTT, TTS, memoria y chat; la configuración pasó a panel lateral con `Modelo/Perfil`, `Audio/TTS`, `PTT`, `YouTube` y `Admin`; `Stream Admin` quedó como workspace administrativo secundario con secciones internas; y los logs se mantienen abajo bajo el switch `Mostrar logs` con tabs `Log General`, `Kira Acciones`, `YT Chat` y `Stream Log`. El plan y riesgos quedaron documentados en `docs/UI_UX_REFACTOR_PLAN.md`.

**[WorkerSeniorAI] Refinamiento de Feedback Visual UI — 2026-05-10:** Se corrigieron múltiples desfases de estado visual. Se implementó rastreo de "Modelo Activo" para desactivar botones de Ollama engañosos, se sincronizó el switch inicial de Logs, se vincularon los botones de LiveAudio a los cambios del WebSocket mediante el `UIState`, se añadió feedback transitorio para la carga de audio WAV ("Cargando..." y "WAV Cargado ✅") y se sincronizó bidireccionalmente el botón de YouTube Chat con el backend de Stream Admin.

**[WorkerSeniorAI] Fix race condition motor/mainloop — 2026-05-11:** Se corrigió `RuntimeError: main thread is not in main loop` difiriendo `motor_ia.start()` con `self.after(100, self._start_motor)` en `ui/app_shell.py`. El hilo arrancaba en `__init__` antes de que `mainloop()` estuviera activo, y cuando `_check_ollama_service()` terminaba rápido llamaba `ui_callback("ready")` → `self.after(0, ...)` → crash.

**[WorkerSeniorAI] Documentación de decisiones y troubleshooting — 2026-05-11:** Se crearon `docs/DECISIONS.md` (7 ADRs: local-first, PTT anti-loop, pausa Stream Admin, TTS ligero sin offline, filtro emotes, race condition fix, single LLM) y `docs/TROUBLESHOOTING.md` (10 bugs resueltos con causa raíz y fix). Se reescribió `docs/architecture.md` con estructura real, threading model completo y contratos de comunicación.

**[WorkerSeniorAI] Cola prioritaria con buffer PTT y acumulación — 2026-05-12:** Se implementó ADR-008: buffer PTT con grace period de 2s, cola prioritaria (PTT > chat, máx 5 items), y buffer de acumulación (50 items, 2000 chars, TTL 2 min) que compacta mensajes descartados en 1 consulta cuando el motor queda libre. Fix del bug TSH-011: PTT no funcionaba porque las transcripciones llegaban después de soltar F8 y se descartaban. Archivos modificados: `ui/voice_control.py`, `core/llm_engine.py`, `ui/app_shell.py`, `ui/smart_aggregator_ui.py`.

**[WorkerSeniorAI] Mitigación de Alucinaciones Whisper — 2026-05-10:** Se implementó una capa de sanitización agresiva (Filtro Anti-Loop) en `ui/voice_control.py` para detectar y deduplicar palabras o frases repetidas consecutivamente más de 3 veces. Esto mitiga el problema de "Secuestro de Atención" en el LLM causado por el bug clásico de transcripción de Whisper (ej. loopeo de ruido de fondo como "gracias gracias gracias"), protegiendo el pipeline conversacional sin matar por completo el contexto.

**[WorkerSeniorAI] Filtros de calidad de chat — 2026-05-12:** Se agregaron 5 filtros nuevos a `MessageFilter` para evitar que basura del chat llegue al modelo: (1) carácter repetitivo >50% descartado (ej: `wwwwwwww`), (2) palabras repetidas >3 veces o ratio único <25% descartado (ej: `fe fe fe fe fe`), (3) gibberish con ratio de vocales <10% descartado (ej: `jsklsbfkfofii`), (4) ASCII art con box-drawing chars o líneas de `===` descartado, (5) quality score para mensajes cortos (≤4 palabras) o baja diversidad que penaliza sin descartar. Config: `min_quality_score: 0.3` en `smart_aggregator.yaml`. Archivos modificados: `smart_aggregator/message_filter.py`, `smart_aggregator/aggregator.py`, `config/smart_aggregator.yaml`. 29/29 tests passing.

**[WorkerSeniorAI] Intent Aggregator portable — 2026-05-12:** Se agregó `smart_aggregator/intent_aggregator.py` para agrupar mensajes filtrados por intención antes de llamar a Kira: saludos/cumpleaños, unirse/jugar, solicitud/amigo, preguntas del juego, videos/collabs, sugerencias, tradeos, feedback y copypasta. Se corrigió el sesgo inicial: nombres propios como streamers o colaboradores ya no son reglas globales; ahora se extraen como entidades dinámicas desde patrones genéricos (`video con X`, `conoces a X`, `juega X`). Kira recibe resumen por temas dominantes en vez de una lista cruda de mensajes. Archivos modificados: `smart_aggregator/intent_aggregator.py`, `smart_aggregator/aggregator.py`, `ui/smart_aggregator_ui.py`, `config/smart_aggregator.yaml`, `tests/test_smart_aggregator.py`.

**[WorkerSeniorAI] Persistencia compacta RF3 — 2026-05-13:** Se corrigió la política de historial del Smart Aggregator: producción no guarda chat crudo masivo. SQLite conserva `context_snapshots`, es decir, el contexto compacto que Kira realmente usó para hablar. Luego se endureció la política: el guardado raw queda prohibido por código, sin modo `persist_raw_messages`; los comentarios se filtran, se compactan en memoria y solo se persisten como contexto. Se agregó `scripts/cleanup_smart_aggregator_db.py` para limpiar historiales viejos; la limpieza local eliminó 239.904 filas raw de `messages` y un `chat_log.jsonl` de 51.681.730 bytes, preservando snapshots compactos.

**[WorkerSeniorAI] Kira Co-host Agenda Mode — 2026-05-13:** Se documentó el feature definitivo para conducción semi-autónoma: Kira no será full autónoma, sino co-host con agenda aprobada por el streamer, state machine determinista, PTT prioritario, chat compactado como señal secundaria, stop suave/emergencia, sanitizer anti-leak y política de fallos/backoff. Ver `docs/KIRA_COHOST_AGENDA_MODE.md` y ADR-013 en `docs/DECISIONS.md`.

**[WorkerSeniorAI] StorageConfig portable — 2026-05-13:** Se agregó `config/storage.py` y `config/storage.yaml` para que el usuario defina en qué disco viven caches y temporales (`TEMP`, `TMP`, `HF_HOME`, `HUGGINGFACE_HUB_CACHE`, `TRANSFORMERS_CACHE`, `TORCH_HOME`, `OLLAMA_MODELS`). El default `auto` mantiene compatibilidad con `modelos_f5`/`temp` del proyecto y respeta `OLLAMA_MODELS` existente; el usuario puede moverlo a `D:/...`, `E:/...` u otro disco sin tocar código. `server_qwen.py`, `config/settings.py` y el arranque de Ollama desde `ui/model_panel.py` ahora usan esta configuración.

---

## 🏗️ Arquitectura Sugerida (Modular Services)

Para garantizar la estabilidad en una **RTX 3060**, se propone una arquitectura de **Microservicios Desacoplados**:

| Componente | Tecnología | Rol |
| :--- | :--- | :--- |
| **Core Orchestrator** | Python / Flask | Gestión de estados, PTT, y lógica de negocio. |
| **LLM Service** | Ollama (Llama 3) | Procesamiento de texto y análisis de sentimiento. |
| **TTS Service** | Qwen3-TTS (Local) | Generación de audio pesado (clonación). |
| **Stream Listener** | WebSockets (Twitch) | Ingesta de chat en tiempo real y filtrado. |
| **Voice Gate** | Silero VAD + Whisper | Transcripción de audio filtrada por hardware. |



---

## 🛠️ Sugerencias para el Agente Constructor

1.  **Manejo de Hilos (Threading):** Es imperativo que el `Global Hotkey Listener` corra en un hilo independiente del `Pygame Mixer` para evitar latencia en la detección de la pulsación.
2.  **Prevención de Bucles (Anti-Loop):** Implementar un "Mute Virtual" por software que limpie el buffer de entrada en el instante exacto en que se presiona el PTT, eliminando residuos de audio previos.
3.  **Persistencia de Contexto:** El historial de chat consolidado debe guardarse en una base de datos ligera (SQLite) para permitir que Kira recuerde temas generales de la audiencia incluso tras un reinicio del servicio.

Este documento sirve como base técnica para la implementación de las siguientes fases del desarrollo de **Kira**.
