# ADR — Auditoría 4R (Risk, Readability, Reliability, Resilience)

| Campo | Valor |
|---|---|
| **Estado** | DIAGNÓSTICO — solo análisis, sin cambios de código |
| **Fecha** | 2026-06-16 |
| **Rama** | `audit/comprehensive-review` |
| **Alcance** | Paquete `opencohost/` (core, ui, smart_aggregator, stream_admin, avatar, config) |
| **Framework** | 4R — Risk · Readability · Reliability · Resilience |
| **Síntesis basada en** | 16 auditorías especializadas + verificación directa de código en esta rama |

> **Nota metodológica.** Cada hallazgo cita `archivo:línea`. Los conteos de líneas y las afirmaciones marcadas como **VERIFICADO** se contrastaron leyendo el archivo en esta rama. Las afirmaciones marcadas como **INFERENCIA** se basan en lectura parcial o en el reporte de los auditores y no se confirmaron de forma exhaustiva; se señalan explícitamente. Donde la propuesta de "arquitectura limpia" de un auditor entra en conflicto con el peso de empaquetado (PyInstaller/Hatchling) o con la thread-safety de CustomTkinter, se marca como **PRAGMATISMO** y se argumenta por qué la refactorización puede ser un defecto y no una mejora.

---

## Resumen ejecutivo

OpenCohost es una aplicación local-first con una arquitectura de hilos correcta en su **diseño nuclear** (el patrón Observer de `UIState` y el marshaling `_safe_after` están bien implementados) pero con **deuda de confiabilidad concentrada en los límites con servicios externos** (Ollama, Qwen-TTS, Edge-TTS, OBS, YouTube) y con **deuda de legibilidad concentrada en dos God Objects**: `VocalAIApp` (`opencohost/ui/app_shell.py`, **3256 líneas — VERIFICADO**) y `MotorVocalIA` (`opencohost/core/llm_engine.py`, **1848 líneas — VERIFICADO**).

Los riesgos materiales para el lanzamiento son, en orden de severidad:

1. **CRÍTICO — Watchdog de inferencia que orfana hilos.** En timeout, el worker thread de Ollama queda colgado sin cancelación (`llm_engine.py:1174-1196` — **VERIFICADO**). En un stream 24/7, timeouts repetidos acumulan hilos bloqueados y conexiones Ollama medio-abiertas.
2. **CRÍTICO — Gate de salud sin timeout dentro del hilo productor de TTS.** `heavy_tts_block_reason()` se invoca sincrónicamente desde el productor sin presupuesto de tiempo (`llm_engine.py:1564-1585` — **VERIFICADO**). Si el health monitor se cuelga, el TTS se bloquea.
3. **ALTO — Tokens OAuth con ventana TOCTOU de permisos.** El archivo se escribe en claro *antes* de restringir permisos, y la restricción traga toda excepción (`stream_admin/oauth_store.py:28-31` y `:55-76` — **VERIFICADO**).
4. **ALTO — Cadena de SPOFs de inicialización silenciosa.** Fallos de init de HealthMonitor / SmartAggregator / StreamAdmin degradan funcionalidad sin indicador en UI (`app_shell.py:201-206` y referencias — **INFERENCIA parcial**).

La buena noticia: la regla de privacidad "nunca exponer chat crudo" **se respeta en múltiples capas** (sanitización en `MessageFilter`, `SessionHistory.get_session_context()` devuelve lista vacía por diseño, validación de tipo en dataclasses de `editorial_cards`). Las consultas SQLite usan parámetros preparados en todo el subsistema de persistencia. Esos cimientos no deben tocarse.

**Recomendación de gate de release:** los dos hallazgos CRÍTICOS de RELIABILITY (watchdog + health gate) deben validarse contra un modelo pesado real (el gate ya pendiente en `AGENT_HANDOFF.md` para `heavy_model_inference_recovery`) antes de declarar readiness. El resto es deuda gestionable post-lanzamiento.

---

## RISK — Puntos únicos de fallo, seguridad local, dependencias críticas en runtime

### R-1. Puntos únicos de fallo (SPOF)

| ID | Hallazgo | Severidad | Ubicación | Estado |
|---|---|---|---|---|
| SPOF-1 | **Ollama como SPOF de toda inferencia LLM.** Endpoint hardcodeado `127.0.0.1:11434`; sin LLM de respaldo. Una caída de Ollama deja el motor colgado hasta el watchdog. | ALTO | `llm_engine.py` (cliente Ollama), `health_monitor.py` (watchdog) | INFERENCIA |
| SPOF-2 | **Qwen-TTS como SPOF de TTS pesado.** `TTS_SERVER_URL` hardcodeado a `127.0.0.1:5000`. El único respaldo es Piper offline, que requiere modelo pre-descargado; si falta, el audio enmudece sin feedback. | ALTO | `config/settings.py` (TTS_SERVER_URL), `llm_engine.py` (path `pesado`) | INFERENCIA |
| SPOF-3 | **Race de arranque motor↔mainloop.** El hilo motor puede emitir eventos antes de que Tkinter entre en `mainloop()`. `_safe_after` captura `RuntimeError` y **descarta el evento silenciosamente**. | ALTO | `app_shell.py:2456-2472` — **VERIFICADO** | VERIFICADO |
| SPOF-4 | **Cadena de inits opcionales sin visibilidad.** Si fallan HealthMonitor, SmartAggregator o StreamAdmin, la referencia queda en `None` y la funcionalidad degrada sin indicador de UI. | ALTO | `app_shell.py:201-206` (health), refs a smart_agg/stream_admin | INFERENCIA |
| SPOF-5 | **DB única compartida** por editorial cards, topic inbox y agenda. Corrupción del archivo derriba toda la capa de datos a la vez. | MED | `config/settings.py` (`EDITORIAL_CARDS_DB`), tres stores apuntan al mismo archivo | INFERENCIA |

**Detalle SPOF-3 (VERIFICADO).** El docstring de `_safe_after` lo admite explícitamente: *"During startup the motor thread may fire events before Tkinter enters mainloop(). self.after() raises RuntimeError in that case. We silently skip."* (`app_shell.py:2456-2462`). Mitigado parcialmente porque el path off-thread encola a `_ui_task_queue` (`app_shell.py:2464-2468`), pero el path final `self.after()` traga `RuntimeError` (`app_shell.py:2470-2472`). Eventos de arranque verdaderamente tempranos (p.ej. "Ollama no disponible" emitido antes del loop) se pierden. **Mitigación de bajo costo:** registrar el evento descartado en un buffer y re-aplicarlo tras `mainloop()`; no requiere rearquitectura.

### R-2. Seguridad local (secrets, paths)

| ID | Hallazgo | Severidad | Ubicación | Estado |
|---|---|---|---|---|
| SEC-1 | **TOCTOU en permisos de tokens OAuth.** `save()`/`delete()` hacen `json.dump()` y *luego* llaman `_restrict_permissions()`. Entre ambos, el archivo es legible con permisos por defecto. `_restrict_permissions()` traga toda excepción (`except Exception: pass`), por lo que si `icacls` falla o expira (timeout 5s), el archivo queda world-readable de forma permanente. | ALTO | `oauth_store.py:28-31` (write) + `:55-76` (chmod/icacls) — **VERIFICADO** | VERIFICADO |
| SEC-2 | **`chmod(0o600)` inefectivo en NTFS.** El propio código lo documenta. En Windows solo `icacls` aplica, y es best-effort. `access_token`, `refresh_token`, `live_chat_id`, `author_channel_id` quedan en JSON plano. | ALTO | `oauth_store.py:55-76` — **VERIFICADO** | VERIFICADO |
| SEC-3 | **`client_secret` en YAML/JSON plano.** El secreto de OAuth cliente se persiste sin cifrado ni derivación de clave. En máquinas corporativas multi-usuario bajo `%APPDATA%`, otro usuario local puede leerlo. | ALTO | `stream_admin/admin_manager.py` (save de config) | INFERENCIA |
| SEC-4 | **Path de voz de referencia como absoluto en request TTS.** `os.path.abspath(self.voz_referencia)` se envía al servidor TTS; filtra estructura del filesystem local en logs/errores. Bajo riesgo mientras el servidor sea localhost. | MED | `llm_engine.py:1497` y request al servidor Qwen | INFERENCIA |
| SEC-5 | **Logs sin rotación.** `LOG_DIR` acumula logs con timestamp sin rotación ni purga; un stream 24/7 puede llenar `%APPDATA%`. | MED | `config/logger.py` (FileHandler), `config/settings.py:LOG_DIR` | INFERENCIA |

**Nota de matiz sobre el filtro de datos sensibles (corrección a la auditoría cruzada).** Uno de los auditores marcó `SensitiveDataFilter` como vector de DoS por backtracking catastrófico de regex (`config/logger.py:15-31`). **Tras leer el código directamente — VERIFICADO — esa afirmación está sobredimensionada.** Los seis patrones usan clases de caracteres *negadas* (`[^'\",}\s]+`, `[A-Za-z0-9._\-]+`) sin cuantificadores anidados ni alternaciones solapadas, que es precisamente la forma que **no** dispara backtracking exponencial. El filtro es correcto y debe conservarse. El riesgo real residual es menor: el filtro muta `record.msg` in-place (`logger.py:29`), lo que puede afectar handlers múltiples sobre el mismo record — pero eso es un detalle, no un DoS.

### R-3. Dependencias externas críticas en runtime

- **Ollama** (proceso separado): sin él, todas las features LLM fallan; el app igual arranca (degradación silenciosa). — INFERENCIA
- **Qwen/F5-TTS** (subproceso en `xtts_env`): requiere entorno conda separado no empaquetado; si falta, el TTS pesado no está disponible. — INFERENCIA
- **OBS Studio + WebSocket**: para control de avatar; `obsws_python` es dependencia opcional importada tardíamente. — INFERENCIA
- **pynvml** (opcional): si falta, el `VRAMGuard` degrada con gracia, pero un auditor reporta que puede informar VRAM=0.0/`critical` en vez de `unavailable`, bloqueando TTS pesado innecesariamente. — INFERENCIA, requiere verificación.

**PRAGMATISMO — no abstraer Ollama/TTS detrás de interfaces "limpias".** Los SPOF-1 y SPOF-2 invitan a "introducir una capa de abstracción de proveedor LLM/TTS". Para una app local-first empaquetada, eso es **sobre-ingeniería con costo neto negativo**: añade superficie de código, no reduce el peso del bundle (las dependencias pesadas se incluyen igual) y no elimina el SPOF real (sigue habiendo un solo Ollama local). La mitigación correcta y barata es **detección + restart/alerta** vía el HealthMonitor que ya existe, no una jerarquía de estrategias.

---

## READABILITY — Complejidad, duplicación, claridad de nombres y flujos

### RD-1. God Objects (complejidad estructural)

| Archivo | Líneas | Concerns fusionados | Severidad | Estado |
|---|---|---|---|---|
| `ui/app_shell.py` (`VocalAIApp`) | **3256** | construcción UI, wiring de paneles, dispatch de eventos motor, coordinación de hilos, máquina de estados de agenda, stream admin, avatar/OBS, audio bed, PTT, cleanup | CRÍTICO | **VERIFICADO** |
| `core/llm_engine.py` (`MotorVocalIA`) | **1848** | orquestación LLM + tier switching, pipeline TTS productor/consumidor, memoria conversacional, priority queue, prefetch de agenda, integración health monitor | ALTO | **VERIFICADO** |
| `smart_aggregator/kira_agenda_controller.py` | **1207** | gestión de tópicos, validación de salida (8 guardrails), política de recovery, métricas, templating de prompts | ALTO | **VERIFICADO** |
| `core/health_monitor.py` | **687** | daemon de polling, ciclo de vida de subproceso Qwen, agregación de salud, gates de fallback | MED | **VERIFICADO** |

**Detalle (VERIFICADO).** `_build_ui()` construye toda la jerarquía de widgets en un solo método de ~570 líneas (`app_shell.py:413-985`). El dispatch de eventos motor (`_handle_motor_event`) enruta 18 tipos de evento. Hay 136+ llamadas a `.after()` a lo largo del archivo (reportado por auditor; orden de magnitud consistente con el tamaño).

### RD-2. Duplicación de patrones

| ID | Hallazgo | Severidad | Ubicación | Estado |
|---|---|---|---|---|
| DUP-1 | **Detección de patrones de chat duplicada.** `MessageFilter` y `ChatEventDetector` definen reglas regex solapadas (preguntas, saludos, correcciones, quejas, moderación) sin librería compartida. Si un patrón cambia, hay que sincronizar a mano dos archivos. | ALTO | `smart_aggregator/message_filter.py` y `chat_input_contract.py` | INFERENCIA |
| DUP-2 | **Lógica de fallback de motor TTS fragmentada en dos módulos.** La decisión de motor pesado/ligero vive a la vez en `llm_engine.py` y en `health_monitor.py`. | MED | `llm_engine.py:1549-1585` (gate aplicado — **VERIFICADO**) + `health_monitor.py` (`heavy_tts_block_reason`) | PARCIAL |
| DUP-3 | **`OBSConfig` definido dos veces.** Dataclass idéntica en `avatar/obs_client.py` y `avatar/avatar_config.py`, sin herencia. | MED | `avatar/obs_client.py` y `avatar/avatar_config.py` | INFERENCIA |
| DUP-4 | **Contadores de fallo duales.** `failure_count` (legacy) y `recovery.failure_count` (canónico) sincronizados a mano; riesgo de divergencia silenciosa. | MED | `kira_agenda_controller.py` (refs a ambos contadores) | INFERENCIA |
| DUP-5 | **`APP_ID` duplicado** entre `server_qwen.py` y `health_monitor.py:239` sin test que los mantenga en sync; una divergencia hace fallar el self-check de salud en silencio. | MED | `server_qwen.py` + `health_monitor.py:239` | INFERENCIA |
| DUP-6 | **Resolución de paths via `__file__.resolve().parent.parent.parent` duplicada** en `settings.py` y `storage.py`; frágil para wheels en `site-packages`. | MED | `config/settings.py` + `config/storage.py` | INFERENCIA |

### RD-3. Claridad de nombres y flujos

- **Mezcla español/inglés en `MotorVocalIA`.** `oraciones`, `fragmentos_brutos`, `cola_audios`, `productor` conviven con `health_monitor`, `effective_motor`, `fallback_reason` en el mismo pipeline (`llm_engine.py`, sección `_hablar`). Aumenta la carga cognitiva en reviews. — VERIFICADO parcialmente (vistos `oraciones`, `effective_motor`, `fallback_reason` en `:1564-1586`).
- **`_on_motor_*` vs `_handle_motor_*`.** Convención inconsistente: `_on_` debería reservarse para callbacks externos y `_handle_` para dispatch interno; el código los mezcla. — INFERENCIA.
- **Mutaciones de estado por tres vías.** Algunas vía `UIState`, otras por asignación directa de atributo, otras por `command_queue.put()`. No hay un patrón único de mutación de estado. — INFERENCIA.
- **Lookups de widgets por string.** `StreamAdminUI` usa `_widget(name)` con dict (113+ sitios reportados); renombrar un widget en `build()` rompe en silencio con `None`. — INFERENCIA.

**PRAGMATISMO — sobre el "refactor a 5-7 clases" del God Object.** Partir `_build_ui()` en factories (`_build_status_bar()`, etc.) **sí** mejora legibilidad y es de bajo riesgo. Pero la propuesta de extraer `UIBuilder`/`MotorCoordinator`/`AgendaCoordinator`/etc. debe sopesarse: el cruce de hilos motor→main pasa por `_safe_after`, y **fragmentar el coordinador multiplica los puntos donde un sub-objeto podría tocar un widget fuera del main thread**. Una refactorización mal ejecutada aquí *introduce* bugs de thread-safety en vez de eliminarlos. Recomendación: extraer **lógica de negocio sin Tk** (agenda, stream admin) a coordinadores testeables, y **dejar el wiring de widgets y el dispatch `_safe_after` en una sola clase** que sea el único dueño del boundary de thread. No es elegante, pero es seguro.

---

## RELIABILITY — Manejo de errores en Ollama/Qwen/F5, resiliencia de init de motores

### RL-1. Comunicación con Ollama

| ID | Hallazgo | Severidad | Ubicación | Estado |
|---|---|---|---|---|
| REL-1 | **CRÍTICO — Watchdog orfana el worker thread.** `_ollama_chat_with_watchdog` lanza un worker `daemon=True` y espera con `done.wait(timeout)`. En timeout, el worker **sigue bloqueado** en `ollama.chat()`; no hay cancelación. Timeouts repetidos acumulan hilos y conexiones medio-abiertas. | CRÍTICO | `llm_engine.py:1174-1196` — **VERIFICADO** | VERIFICADO |
| REL-2 | **Watchdog de grano grueso.** Un único timeout (`OLLAMA_CHAT_TIMEOUT`, reducido a `min(timeout, 45.0)` post-switch) para todos los modelos. Un modelo de razonamiento (Qwen3) que piensa 60s puede expirar a los 45s aunque funcione bien. | ALTO | `llm_engine.py:121-122` — **VERIFICADO** | VERIFICADO |
| REL-3 | **Reintentos solo en respuesta vacía, no en errores de transporte transitorios.** El loop (`max_intentos=2`) reintenta si el contenido viene vacío, pero un 500 de Ollama o un blip de red devuelve cadena vacía de inmediato sin reintento real, y sin backoff exponencial. | ALTO | `llm_engine.py:1041-1096` | INFERENCIA |
| REL-4 | **Lecturas de estado de modelo sin lock durante el trace.** `current_model`/`_desired_model`/`_loaded_model` se leen sin `_lock` para el `MODEL_TRACE`; bajo `switch_llm_tier` concurrente el trace puede reportar un snapshot imposible. La llamada de inferencia en sí usa un snapshot, así que el audio es correcto; el problema es de observabilidad. | MED | `llm_engine.py:1101-1114` | INFERENCIA |

### RL-2. Comunicación con Qwen / F5 (TTS pesado)

| ID | Hallazgo | Severidad | Ubicación | Estado |
|---|---|---|---|---|
| REL-5 | **CRÍTICO — Gate de salud sin timeout en el hilo productor.** `heavy_tts_block_reason()` se llama sincrónicamente desde el productor TTS sin presupuesto de tiempo. Si el health monitor se cuelga (polling de VRAM/Ollama/Qwen), el productor se bloquea, la cola se llena, el consumidor espera, y el TTS enmudece. | CRÍTICO | `llm_engine.py:1564-1585` — **VERIFICADO** | VERIFICADO |
| REL-6 | **Caída silenciosa de chunks si Piper no está disponible.** En fallback a Piper, si `synthesize()` falla se encola `None` y el consumidor lo trata como chunk saltado; una respuesta de 10 frases pierde una sin re-síntesis ni tono de relleno. | ALTO | `llm_engine.py` (path de fallback Piper, ~`:1600-1722`) | INFERENCIA |
| REL-7 | **Init de Qwen sin reintento.** `QwenProcessManager.start()` tiene deadline duro; sin lógica de restart automático tras timeout. | MED | `health_monitor.py:318-332` | INFERENCIA |
| REL-8 | **`PiperEngine.load()` traga todas las excepciones.** `FileNotFoundError` y crash de ONNX runtime se colapsan a `return False`; el llamador no distingue "falta el archivo" de "ONNX caído". | MED | `core/tts_piper.py:74-87` | INFERENCIA |

### RL-3. Resiliencia de inicialización de motores

| ID | Hallazgo | Severidad | Ubicación | Estado |
|---|---|---|---|---|
| REL-9 | **Init de HealthMonitor opcional con degradación silenciosa.** Si falla, se loguea como no-fatal y el fallback de TTS del motor queda deshabilitado sin indicador en UI. | ALTO | `app_shell.py:201-206` | INFERENCIA |
| REL-10 | **Inversión de callback UI desde daemon — matizado.** Los auditores marcaron como CRÍTICO que `MotorVocalIA` (daemon) invoca `ui_callback` directamente violando thread-safety de CTk (`llm_engine.py:84`, `:273`, etc.). **Tras verificar — VERIFICADO — el matiz importa:** el `ui_callback` cableado es `_on_motor_event`, que **comienza chequeando el hilo** (`app_shell.py:2474-2475`) y enruta a `_safe_after`. El cruce de hilo *está manejado* en el lado receptor. El riesgo real es estructural: el contrato no está documentado y un futuro callback que toque un widget sin pasar por `_safe_after` sí deadlockearía. No es un bug activo; es una trampa latente. | MED | `llm_engine.py:84,273` + `app_shell.py:2474` — **VERIFICADO** | VERIFICADO |
| REL-11 | **Errores de observer tragados sin log.** El loop de dispatch de `UIState` captura toda excepción del callback y hace `pass` sin loguear; un handler de panel que crashea deja la UI sin actualizar y sin rastro. | MED | `ui/state.py` (dispatch loop, ~`:194-199`) | INFERENCIA |

**PRAGMATISMO — la inversión de callback NO requiere reescritura a "event queue pura".** El reporte sugiere que el motor "debería encolar a una cola thread-safe en vez de invocar directamente". Pero eso **ya ocurre** efectivamente: `_on_motor_event` re-marshala vía `_safe_after`→`_ui_task_queue`. Reescribir el motor para que él encole duplicaría el mecanismo. La acción correcta y barata es **documentar el contrato** ("todo `ui_callback` DEBE asumir contexto daemon y no tocar widgets") y, opcionalmente, un assert de thread en debug. Reescribir el sistema de eventos por "limpieza" es riesgo sin retorno.

---

## RESILIENCE — Comportamiento ante fallos inesperados en runtime

### RS-1. Caídas de OBS

| ID | Hallazgo | Severidad | Ubicación | Estado |
|---|---|---|---|---|
| RES-1 | **Loop de reconexión OBS sin tope ni backoff.** El daemon de reconexión reintenta cada 5s indefinidamente; si OBS está caído permanentemente, el hilo gira por siempre. | MED | `app_shell.py:2770-2817` (loop reconexión) | INFERENCIA |
| RES-2 | **`disconnect()` de OBS puede colgar el teardown.** El hilo daemon de OBS puede bloquearse en `connect()`/`disconnect()` si OBS no responde; sin timeout en el join, el cierre del app puede demorar. | MED | `app_shell.py:2753-2768`, `avatar/obs_client.py` | INFERENCIA |
| RES-3 | **Pérdida de conexión OBS a mitad de update.** Si OBS cae entre `get_input_settings` y `set_input_settings`, la excepción se captura pero `on_state_change()` retorna en silencio; sin reconexión automática hasta el próximo cambio de estado. | MED | `avatar/obs_client.py:142-184` | INFERENCIA |
| RES-4 | **Sin verificación de certificado en WebSocket OBS.** `obsws_python` por defecto no verifica TLS; MITM posible en redes no confiables (bajo riesgo en localhost). | BAJO | `avatar/obs_client.py` (ReqClient) | INFERENCIA |

### RS-2. Cuelgues del proceso de inferencia de modelos pesados

| ID | Hallazgo | Severidad | Ubicación | Estado |
|---|---|---|---|---|
| RES-5 | **CRÍTICO (cruza con REL-1/REL-5) — Cuelgue de modelo pesado bloquea el pipeline.** Un modelo que se cuelga dispara el watchdog (orfanando el hilo, REL-1) y, en el path pesado, el gate de salud sin timeout (REL-5) puede bloquear el productor. **Este es el gate de validación de runtime pendiente** según `AGENT_HANDOFF.md` / `CLAUDE.md`. | CRÍTICO | `llm_engine.py:1174-1196` + `:1564-1585` — **VERIFICADO** | VERIFICADO |
| RES-6 | **Kill forzado de Qwen puede dejar VRAM/zombie.** En Windows, `proc.kill()` es asíncrono; `proc.wait(timeout)` puede expirar dejando zombie, y un proceso Torch killed puede no liberar VRAM de inmediato. | MED | `health_monitor.py:334-382` | INFERENCIA |
| RES-7 | **Hilo de prefetch sin join en shutdown.** El `_prefetch_thread` daemon no se joinea explícitamente; si está en inferencia al cerrar, se mata sin cerrar la conexión Ollama. | MED | `llm_engine.py` (prefetch, refs `:415,423`) — worker daemon **VERIFICADO** | PARCIAL |
| RES-8 | **Race en `_speaking` durante `emergency_stop`.** El productor chequea `if not self._speaking` sin lock (`llm_engine.py:1586`) tras pasar el gate; un `emergency_stop` concurrente puede limpiar el flag después del chequeo, permitiendo que chunks ya encolados sigan sonando tras el botón de stop. | MED | `llm_engine.py:1586` (`if not self._speaking: break`) — **VERIFICADO** | VERIFICADO |

### RS-3. Caídas de red al inicializar fallbacks offline

| ID | Hallazgo | Severidad | Ubicación | Estado |
|---|---|---|---|---|
| RES-9 | **Latch de Edge-TTS offline sin lock.** `_edge_tts_offline` se setea en error de conexión y se lee en fast-path sin lock; ventana de race donde un hilo lee `False` justo cuando otro lo setea `True`. El resultado es benigno (reintenta Piper), pero la decisión queda stale. | MED | `llm_engine.py` (path Edge-TTS, ~`:1619-1700`) | INFERENCIA |
| RES-10 | **Doble fallback fallido = silencio total.** Si Edge-TTS está offline y Piper no está disponible (modelo faltante), los chunks se descartan sin audio ni feedback al operador. | MED | `llm_engine.py` (cadena de fallback TTS) | INFERENCIA |
| RES-11 | **`StreamingSpeechPipeline` sin manejo de errores ni backpressure.** Cualquier excepción en `llm.stream()` o `playback.speak()` se propaga sin captura; sin límite de cola. | MED | `core/streaming_speech.py:16-19` | INFERENCIA |
| RES-12 | **Reconexión de chat (YouTube/Twitch) no notifica agotamiento.** Tras `max_retries`, el source pone `_running=False` pero el callback de error se llama por-excepción, no en el agotamiento final; el operador puede no enterarse de que el chat murió. | MED | `smart_aggregator/chat_source.py:118-171` | INFERENCIA |

**PRAGMATISMO — backpressure y timeouts SÍ valen el costo aquí.** A diferencia de las abstracciones de proveedor (que rechacé arriba), agregar **un timeout acotado alrededor de las llamadas a `health_monitor` (REL-5) y un mecanismo de cancelación/marca para el worker de Ollama (REL-1)** es directamente proporcional a la confiabilidad del producto en vivo. No infla el bundle, no toca el main thread de CTk, y ataca los dos CRÍTICOS. Es la inversión de mayor ROI de toda esta auditoría.

---

## Matriz consolidada de severidad

| Severidad | Risk | Readability | Reliability | Resilience | Total |
|---|---|---|---|---|---|
| CRÍTICO | 0 | 1 (RD-1: app_shell) | 2 (REL-1, REL-5) | 1 (RES-5) | **4** |
| ALTO | 4 | 2 | 4 | 0 | **10** |
| MEDIO | 2 | 8 | 3 | 10 | **23** |
| BAJO | 0 | 0 | 0 | 1 | **1** |

> Los CRÍTICOS de RELIABILITY y RESILIENCE (REL-1, REL-5, RES-5) son el mismo cuello de botella visto desde tres ángulos: **la falta de timeout/cancelación en el camino de inferencia pesada y su gate de salud.** Es un único frente de trabajo, y es el gate de release.

---

## Recomendaciones priorizadas (sin implementar — solo diagnóstico)

1. **[GATE DE RELEASE]** Validar REL-1 + REL-5 + RES-5 contra un modelo pesado real que se cuelgue. Acotar con timeout las llamadas a `health_monitor` desde el productor (`llm_engine.py:1564-1585`) y añadir cancelación/marca al worker del watchdog (`llm_engine.py:1174-1196`).
2. **[SEGURIDAD]** Cerrar el TOCTOU de OAuth: crear el archivo de tokens con permisos restringidos *antes* de escribir (p.ej. `os.open(..., 0o600)`), y dejar de tragar la excepción de `_restrict_permissions` (`oauth_store.py:28-31`, `:55-76`).
3. **[OBSERVABILIDAD]** Reemplazar los `except Exception: pass` silenciosos por logging en: dispatch de `UIState`, `_safe_after`, init de HealthMonitor, y cleanup de paneles.
4. **[LEGIBILIDAD, BAJO RIESGO]** Extraer la lógica de negocio **sin Tk** (agenda, stream admin) de `app_shell.py` a coordinadores testeables, conservando un único dueño del boundary `_safe_after`.
5. **[NO HACER]** No abstraer Ollama/TTS detrás de interfaces de proveedor; no reescribir el sistema de eventos motor→UI. Ambas son sobre-ingeniería con costo neto negativo para una app local-first empaquetada.

---

## Apéndice — Afirmaciones load-bearing que un verificador debe re-confirmar

- **REL-1 (orfanato de hilo):** confirmar que en `llm_engine.py:1192` (`done.wait(timeout=...)`) no existe ningún mecanismo posterior que joinee o cancele el worker tras timeout. **(Verificado en esta auditoría; re-confirmar que no hay limpieza diferida en otra parte del archivo.)**
- **REL-5 (gate sin timeout):** confirmar que `heavy_tts_block_reason()` en `health_monitor.py` no tiene timeout interno propio y que se invoca sincrónicamente en el hilo `productor` (`llm_engine.py:1568-1572`). **(Verificado el call-site; no se leyó la implementación completa del método en health_monitor.)**
- **SEC-1 (TOCTOU OAuth):** confirmado el orden write-then-restrict en `oauth_store.py:28-31` y el `except Exception: pass` en `:75-76`. **(Verificado.)**
- **REL-10 (callback daemon):** confirmado que `_on_motor_event` chequea el hilo (`app_shell.py:2474-2475`) — la afirmación CRÍTICA de los auditores está **matizada a MEDIO**. Re-confirmar que ningún otro `ui_callback(...)` del motor toca widgets directamente sin pasar por `_safe_after`.
- **SPOF-3 (race de arranque):** confirmado el `except RuntimeError: pass` en `_safe_after` (`app_shell.py:2470-2472`).
