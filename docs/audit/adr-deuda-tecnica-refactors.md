# ADR — Deuda Técnica y Refactorizaciones (Bloque 2)

| Campo | Valor |
|---|---|
| **Estado** | DIAGNÓSTICO — solo análisis, sin cambios de código |
| **Fecha** | 2026-06-16 |
| **Rama** | `audit/comprehensive-review` |
| **Alcance** | paquete `opencohost/` |
| **Tipo** | Reporte técnico diagnóstico / ADR de deuda |

> **Nota de método.** Cada afirmación no trivial cita `archivo:línea`. Las citas marcadas como *verificadas* fueron confirmadas leyendo el código real durante esta auditoría. Las que provienen únicamente de la síntesis de los 16 auditores se marcan como *(reportado)* y se debe confirmar la línea exacta antes de actuar. Donde una severidad de los auditores estaba **inflada**, lo digo explícitamente y bajo la severidad con evidencia. En esta revisión se corrigieron varias afirmaciones que estaban *sobre-estimadas* respecto del código real (gate de salud, TOCTOU de `_speaking`, lazy-layout de CustomTkinter, estado de `streaming_speech.py`).

---

## Resumen ejecutivo

El sistema es funcional y está razonablemente protegido en sus límites críticos (chat crudo nunca llega al LLM, marshaling a hilo principal vía `_safe_after`, gates de salud para TTS pesado). La deuda técnica real se concentra en **cuatro God classes** y en **un puñado de defectos puntuales de concurrencia y recursos** que sí afectan estabilidad en streams largos (4-8 h). El resto de los hallazgos de los auditores son, en su mayoría, mejoras de "diseño bonito" cuyo ROI es bajo o negativo para una app local-first empaquetada con PyInstaller.

**Lo que SÍ vale la pena (orden de prioridad):**

1. **Gate de salud síncrono sin presupuesto de tiempo explícito** en el productor de TTS (`llm_engine.py:1569`). El riesgo verificado es **acotado** (ver §1.2): la llamada es *side-effect-free* y solo lee un snapshot bajo lock, no hace I/O. El fix (timeout defensivo) es barato y de bajo riesgo, pero la severidad NO es tan alta como se creía: es defensa en profundidad, no un bug activo de stall.
2. **Hilos daemon sin shutdown ordenado** (motor, prefetch, OBS retry, chat poller). En streams largos producen latencia de cierre, sockets huérfanos y posible fuga de threads tras watchdog timeout. (Nota: `HealthMonitor` SÍ tiene shutdown ordenado — ver §1.2.)
3. **`rejection_log` sin eviction** (`kira_agenda_controller.py:385`) y `command_queue` sin límite (`llm_engine.py:88`): crecimiento de memoria no acotado en operación 24/7. ROI cuantificado en §3.4.
4. **Mutación externa del state machine de agenda** desde la UI (`app_shell.py:1367`): rompe la encapsulación del controlador y dispersa la lógica de transiciones.

**Lo que NO vale la pena ahora (sobre-ingeniería o riesgo de empaquetado):**

- Partir las God classes en 5-7 clases "limpias" de golpe. Es deuda real, pero un refactor masivo en plena fase de validación de runtime introduce más riesgo de regresión del que resuelve. Recomendación: extracción incremental y guiada por bugs, no big-bang.
- Adaptadores/facades para `pygame.mixer`, abstracción sobre Ollama, registry de proveedores TTS: añaden capas que inflan el bundle y dificultan que PyInstaller resuelva imports, sin resolver ningún problema real del producto actual.
- "Corregir" la supuesta inversión daemon→UI como si fuera CRITICAL: está mitigada en la práctica (ver §1.1). Tratarla como crítica desviaría esfuerzo del defecto real.

---

## 1. Clases/módulos que necesitan refactor — priorizado

### Tabla maestra de prioridad

| # | Unidad | LOC (verificado) | Concern principal | Severidad real | ROI |
|---|---|---|---|---|---|
| P1 | `core/llm_engine.py` — `MotorVocalIA` | 1848 | God class + concurrencia TTS | **Alta** | Real (parcial) |
| P2 | `ui/app_shell.py` — `VocalAIApp` | 3256 | God class + wiring + coordinación de hilos | **Alta** | Real (parcial) |
| P3 | `smart_aggregator/kira_agenda_controller.py` | 1207 | State machine disperso + métricas sin acotar | **Media-Alta** | Real |
| P4 | `ui/stream_admin_ui.py` — `StreamAdminUI` | 1659 | God class + código muerto (RF4) + lookups por string | **Media** | Mixto |
| P5 | `smart_aggregator/aggregator.py` — `Aggregator` | 458 | God object de pipeline de chat | **Media** | Nice-to-have |
| P6 | `ui/cohost_agenda_panel.py` | 907 | God object de UI (form+parse+render) | **Media** | Nice-to-have |
| P7 | `config/settings.py` | 493 | Mezcla de 3-4 concerns + supresión de excepciones | **Media** | Real (la parte de error-handling) |

LOC verificadas con `wc -l` sobre el árbol real (`app_shell.py`=3256, `llm_engine.py`=1848, `kira_agenda_controller.py`=1207, `stream_admin_ui.py`=1659, `aggregator.py`=458, `cohost_agenda_panel.py`=907, `settings.py`=493).

---

### P1 — `MotorVocalIA` (`core/llm_engine.py`, 1848 líneas) — REFACTOR URGENTE (acotado)

**Por qué es la prioridad 1.** Es el corazón del runtime: orquesta LLM (Ollama), pipeline TTS productor/consumidor, memoria conversacional, priority queue, prefetch de agenda y el bridge de health monitor — **seis concerns ortogonales con cinco locks independientes** (`_lock`, `_history_lock`, `_pq_lock`, `_accum_lock`, `_prefetch_lock`). Viola SRP de forma clara y, más importante, **los bugs de concurrencia que más impactan al producto viven aquí**.

**Violación SOLID.** Single Responsibility (6 razones para cambiar) e Interface Segregation (`app_shell` depende de métodos de los 6 concerns aunque solo use 2-3).

**Defectos puntuales que justifican tocarlo (no la estética):**

- **Gate de salud síncrono sin presupuesto de tiempo — fix barato, severidad acotada (ver §1.2).** *Verificado* en `llm_engine.py:1564-1582`: dentro del productor de TTS, cuando `effective_motor == "pesado"`, se llama `hm.heavy_tts_block_reason(...)` (línea 1569) de forma síncrona. La llamada es *side-effect-free* (documentado en `health_monitor.py:622`) y solo lee `self.state` (un snapshot bajo `_lock`, `health_monitor.py:590-605`) sin hacer I/O. El I/O de polling (VRAM/Ollama/Qwen) corre en el hilo del propio `HealthMonitor`, no en esta llamada. Por lo tanto **no hay un stall real de pipeline a la espera de polling**; el único punto de bloqueo posible es la contención por `_lock`, que es de microsegundos. El fix (envolver en timeout defensivo o decisión de fallback por defecto) sigue valiendo como defensa en profundidad, pero la severidad correcta es **Media**, no Alta. ROI: real (fix trivial), riesgo del defecto: bajo.
- **God function `_generar_dialogo` (~209 líneas)** *(reportado, `llm_engine.py:951-1159`)*: ensambla prompt + snapshot de historial + inyección editorial + digest + llamada Ollama + watchdog + sanitización + guardrail + commit de historial en un solo método. Difícil de testear y de razonar bajo contención de locks.
- **Watchdog timeout deja el worker thread colgado** *(reportado, `llm_engine.py:1174-1196`)*: `done.wait(timeout)` expira pero el thread daemon sigue bloqueado en `ollama.chat()`. Timeouts repetidos en un stream 24/7 acumulan threads. Es una fuga lenta real, no estética.
- **Inversión daemon→UI: ver §1.1 — severidad bajada.**

**Recomendación de ROI.** NO partir la clase en 6 módulos de golpe durante la fase de validación. SÍ extraer primero los dos defectos accionables (timeout defensivo del gate, shutdown del watchdog) como cambios quirúrgicos. La descomposición en `LLMOrchestrator` / `TTSPipeline` / `ConversationMemory` se justifica **después** de que el runtime esté validado, y de forma incremental.

#### 1.1 — Corrección a los auditores: la "inversión daemon→UI" NO es CRITICAL

Varios auditores marcaron como CRITICAL que `MotorVocalIA` (daemon, `daemon=True` *verificado* en `llm_engine.py:85`) invoca `self.ui_callback(...)` desde el hilo daemon (*verificado*: 30+ llamadas, p. ej. `llm_engine.py:273, 1489, 1532`), prediciendo "deadlock o corrupción de CustomTkinter".

**Esto está mitigado en la práctica y la severidad CRITICAL es incorrecta.** *Verificado*:

- El `ui_callback` está cableado a `self._on_motor_event` (`app_shell.py:190`).
- `_on_motor_event` (`app_shell.py:2474-2478`) detecta si está fuera del hilo principal y **reencola vía `_safe_after`** antes de tocar nada.
- `_safe_after` (`app_shell.py:2456-2472`) encola en `_ui_task_queue` cuando `current_thread() is not main_thread()`.
- El motor solo pasa **strings de estado** (`"processing"`, `"speaking_start"`, …), nunca widgets ni objetos Tk.

Es decir: ningún `.configure()`/`.grid()` corre en el hilo daemon. El patrón de marshaling es correcto. **El defecto REAL aquí es menor**: `_safe_after` traga `RuntimeError` con `except: pass` (`app_shell.py:2471` *verificado*) durante la carrera de arranque, descartando eventos sin log. Severidad: media (observabilidad), no crítica. Fix barato: loguear a DEBUG y/o bufferizar eventos perdidos hasta que el mainloop arranque.

#### 1.2 — Verificación del riesgo P1: ¿es el `HealthMonitor` un punto de bloqueo real?

Esta sub-sección documenta la verificación pedida explícitamente: si el gate de salud puede colgar el productor de TTS, el `HealthMonitor` debe ser el sospechoso. **No lo es, y aquí está la evidencia leída del código.**

- **`HealthMonitor` es daemon con shutdown ordenado.** *Verificado*: `super().__init__(name="HealthMonitor", daemon=True)` (`health_monitor.py:501`). `stop()` setea `_stop_event`, llama `self._qwen.stop()` y hace `self.join(timeout=5)` (`health_monitor.py:528-532`). Es decir, a diferencia de otros daemons del repo, este SÍ tiene join acotado en teardown.
- **El bucle de polling corre en su propio hilo, no en la ruta del productor.** *Verificado*: `run()` itera `while not self._stop_event.is_set(): self._poll_all(); self._stop_event.wait(HEALTH_POLL_INTERVAL)` (`health_monitor.py:511-521`). Todo el I/O (VRAM, Ollama, Qwen) ocurre dentro de `_poll_all` (`health_monitor.py:534-555`), en el hilo del monitor.
- **Intervalos de polling razonables (< 15 s).** *Verificado* en `config/settings.py`: `HEALTH_POLL_INTERVAL = 5`, `VRAM_POLL_INTERVAL = 10`, `OLLAMA_POLL_INTERVAL = 15`, `OLLAMA_REQUEST_TIMEOUT = 5`, `QWEN_STARTUP_TIMEOUT = 60`.
- **Todas las llamadas de red tienen `timeout` explícito.** *Verificado*:
  - Ollama `/api/tags`: `requests.get(..., timeout=OLLAMA_REQUEST_TIMEOUT)` = 5 s (`health_monitor.py:135-137`).
  - Qwen `_check_health`: `requests.get(self.HEALTH_URL, timeout=3)` = 3 s (`health_monitor.py:442`).
  - Sonda de puerto Qwen: `socket.connect_ex(("127.0.0.1", port))` con context manager (`health_monitor.py:464-467`) — `connect_ex` no espera datos, retorna de inmediato; no hay `recv()` sin timeout.
  - VRAM vía pynvml: llamada local in-process, sin red (`health_monitor.py:81-104`).
  - Subprocesos Qwen: todos los `proc.wait(...)` usan `timeout=` (`health_monitor.py:366, 378, 381`).
- **El gate consultado por el productor es un snapshot, no I/O.** *Verificado*: `heavy_tts_block_reason` está documentado como *side-effect-free* (`health_monitor.py:622`) y solo lee `self.state` (`:634`). La propiedad `state` adquiere `_lock` únicamente para **copiar campos** del dataclass (`:590-605`); no hace red, sockets ni subprocess bajo el lock.

**Conclusión verificada:** el peor caso del gate en `llm_engine.py:1569` es una contención por `_lock` de microsegundos mientras el hilo del monitor copia/escribe el snapshot, NO un stall a la espera de polling de red. El timeout defensivo se mantiene como recomendación (cinturón y tirantes), pero la afirmación previa de "stall de audio en vivo si el HM se cuelga (polling de VRAM/Ollama)" estaba **sobre-estimada** y se corrige aquí.

---

### P2 — `VocalAIApp` (`ui/app_shell.py`, 3256 líneas) — REFACTOR URGENTE (acotado)

**Por qué.** Es la God class más grande del repo: construcción de UI, wiring de 10+ paneles, dispatch de eventos del motor, máquina de estados de agenda, lógica de stream admin, integración OBS/avatar, control de audio bed, PTT, logging y cleanup. SRP violado con ~11 razones para cambiar.

**Defectos puntuales accionables (no la estética del tamaño):**

- **Mutación externa del state machine de agenda.** *Verificado*: `app_shell.py:1367` hace `self.kira_agenda.state = AgendaState.OFF`, y la UI **lee** el estado del controlador en al menos 8 sitios (`app_shell.py:1315, 1323, 1363, 1375, 1541, 1543, 1569, 2205` *verificado*). La capa de presentación está pilotando la máquina de estados interna. Esto, combinado con P3, hace que un freeze de agenda requiera rastrear motor callbacks + UI callbacks + controller. ROI: real (debuggability + corrección).
- **Coordinación de 5+ hilos daemon sin join con timeout** *(reportado, `app_shell.py:219-225, 1742-1746, 1862-1890, 226, 287`)*: agenda loader, OBS retry, chat poller, motor, health monitor. El teardown setea cancel events pero no fuerza join acotado → cierre lento y sockets huérfanos en empaquetado. (Excepción verificada: `HealthMonitor.stop()` SÍ hace `join(timeout=5)`, ver §1.2; el resto de daemons no.)
- **`_build_ui()` de ~570 líneas** *(reportado, `app_shell.py:413-985`)*: construye toda la jerarquía en un método. Severidad alta de legibilidad, **pero ROI bajo**: Tkinter no soporta hot-reload, así que extraer factories solo ayuda al leer; no es urgente.

**Recomendación de ROI.** El verdadero candidato a extracción es la **coordinación de la agenda** (junto con P3): mover el tick loop, prefetch y consumo de chat a un `AgendaOrchestrator` reduce la deuda de P2 y P3 a la vez. El resto del tamaño de `app_shell` es deuda tolerable mientras un solo ingeniero lo conozca; partirlo entero ahora es riesgo de regresión sin valor para el usuario.

---

### P3 — `KiraAgendaController` (`smart_aggregator/kira_agenda_controller.py`, 1207 líneas) — REFACTOR (real)

**Por qué.** Disciplina de límites **excelente** (el controlador es determinista y está aislado de Ollama/TTS/OBS — esto es un acierto, no deuda). El problema es interno: lógica de transiciones de estado dispersa entre `mark_generation_accepted()`, `mark_speech_complete()`, `register_failure()` *(reportado, líneas 769-834)* y la mutación externa desde `app_shell` (§P2). No hay un único guard de transición.

**Defectos puntuales accionables:**

- **`rejection_log` sin eviction — fuga de memoria real en 24/7.** *Verificado*: es un `list` plano (`kira_agenda_controller.py:385`), se appendea por cada rechazo (`905`) y `get_metrics()` lo **itera completo** (`922`). En un stream de 8 h con rechazos frecuentes, crece sin cota y cada cálculo de métricas es O(n). Fix barato y de alto valor: `deque(maxlen=1000)`. ROI: real, cuantificado en §3.4.
- **`_character_repair_needed` no se resetea al aceptar** *(reportado, líneas 701/810/824 vs. accept)*: el flag de reparación queda activo tras un output correcto, inyectando prefijo de reparación innecesario turno tras turno. Desperdicio de tokens + logs confusos. ROI: real, fix trivial.
- **Dual failure counters** (`failure_count` + `recovery.failure_count`, *reportado* líneas 370/808-809): sincronización manual frágil. ROI: nice-to-have (alias por `@property`).

**Recomendación de ROI.** Extraer `AgendaOrchestrator` (compartido con P2) + arreglar los dos defectos de estado (eviction, repair flag) es el mejor retorno de toda la auditoría de agenda. La separación de cada guardrail en objetos `Guardrail` componibles que proponen algunos auditores es **nice-to-have**: el ahorro de "24 escaneos por turno" es real pero no es el cuello de botella del producto.

---

### P4 — `StreamAdminUI` (`ui/stream_admin_ui.py`, 1659 líneas) — REFACTOR (mixto)

**Por qué.** God class de 110 métodos *(reportado)* mezclando OAuth, metadata, chat, moderación y analítica. Dos sub-problemas con ROI distinto:

- **Código muerto RF4 detrás de `STREAM_ADMIN_ENABLED=False`** *(reportado, líneas 365, 438, 493)*: 200+ LOC inalcanzables en el binario. ROI **real pero modesto**: ~20 KB de bloat + carga de revisión. Quitarlo o moverlo a módulo opcional es limpio.
- **Lookups de widgets por string** (`_widget(name)`, 113+ sitios *reportado*): renombrar un widget rompe en silencio (devuelve `None` → `AttributeError` aguas abajo). ROI real como hazard de refactor; un `TypedDict`/dataclass de widgets lo cerraría.

**Recomendación de ROI.** Eliminar RF4 muerto SÍ (limpieza barata, mejora empaquetado). Partir en `OAuthManager`/`MetadataManager`/etc. es deuda legítima pero **no urgente** dado que la feature está congelada por flag.

---

### P5/P6/P7 — Menor prioridad

- **`Aggregator` (`aggregator.py`, 458 líneas)**: God object de 6 dominios *(reportado)*. Acierto de seguridad **verificado** por los auditores (chat crudo no se persiste ni llega al LLM). El refactor a componentes inyectables es nice-to-have; el riesgo de tocarlo > valor actual.
- **`CohostAgendaPanel` (907 líneas)**: mezcla form/parse/render *(reportado)*. Deuda de UI tolerable; split solo si se va a iterar fuerte sobre ese panel.
- **`settings.py` (493 líneas)**: mezcla paths + tuning LLM + TTS + thresholds. La parte que SÍ vale arreglar es la **supresión silenciosa de excepciones** (ver §2.4), no la división en módulos.

---

## 2. Deficiencias técnicas puntuales (file:line)

### 2.1 — Concurrencia en UI con hilos de Python

| Defecto | Ubicación | Estado | Severidad | Nota |
|---|---|---|---|---|
| Gate de salud síncrono sin timeout defensivo (riesgo acotado, ver §1.2) | `core/llm_engine.py:1564-1582` (llamada en `:1569`) | **Verificado** | Media | No hace I/O; solo lee snapshot bajo lock. Fix = defensa en profundidad |
| `_safe_after` traga `RuntimeError` sin log (carrera de arranque) | `core/llm_engine.py` → `ui/app_shell.py:2469-2472` | **Verificado** | Media | Eventos de motor perdidos en silencio al arrancar |
| Watchdog timeout deja worker thread daemon colgado | `core/llm_engine.py:1174-1196` | Reportado | Alta | Fuga lenta de threads en 24/7 |
| `command_queue` sin `maxsize` → sin backpressure | `core/llm_engine.py:88` | **Verificado** (es `queue.Queue()`) | Media | OOM bajo ráfaga (PTT+WS+chat) si el motor se atasca |
| TOCTOU sobre `_speaking` entre productor y limpieza externa | `core/llm_engine.py:1587` (read sin lock) | **Verificado** | Baja (justificado, ver §2.5) | El consumidor re-chequea bajo lock; silencio se garantiza en ~50 ms |
| Mutación externa del state machine de agenda desde UI | `ui/app_shell.py:1367` (`= AgendaState.OFF`) | **Verificado** | Media | Rompe encapsulación; transiciones dispersas |
| Lecturas de estado de agenda sin lock desde hilo Tk | `ui/app_shell.py:1315,1323,1363,1375,1541,1543,1569,2205` | **Verificado** | Baja | OK si el controlador es main-thread-only; falta documentarlo |
| Health monitor: loops de polling internos | `core/health_monitor.py:511-555` | **Verificado** | Baja | SÍ tiene shutdown ordenado (`stop()`+`join(timeout=5)`, `:528-532`) y timeouts en todo I/O (ver §1.2) |
| Lifecycle de QwenProcessManager escrito fuera de lock en handlers | `core/health_monitor.py:334-416` | Reportado | Media | Lectura inconsistente de `state` |
| Timer de idle de AudioBed puede correr tras `stop()` | `core/audio_bed.py:107-110, 120-132` | Reportado | Baja | Frágil, no es fuga (RLock + main thread único) |

**Patrón positivo a preservar (no es deuda):** el marshaling vía `_safe_after`/`_schedule_ui_update` está bien aplicado y es la razón por la que la app funciona pese a 5+ daemons. No introducir un "event bus" que lo reemplace; eso es complejidad sin retorno.

### 2.2 — Leaks de recursos

| Defecto | Ubicación | Estado | Severidad |
|---|---|---|---|
| `rejection_log` sin cota → memoria + O(n) en métricas | `smart_aggregator/kira_agenda_controller.py:385,905,922` | **Verificado** | Media |
| Subproceso OllamaStartupManager no terminado en `cleanup()` (zombies) | `ui/model_panel.py:685-717` | Reportado | Media |
| File handles de logs de subproceso Qwen sin cierre garantizado | `core/health_monitor.py:250-265,301-302` | Reportado | Media (alta en 4-8 h) |
| Hilo de prefetch del motor sin join en shutdown | `core/llm_engine.py:176-179` | Reportado | Media |
| Logs de app/subproceso sin rotación (crecimiento ilimitado) | `config/logger.py:34-36` | Reportado | Media |
| Timers daemon de AudioBed/prefetch cancelados con `except: pass` | `core/audio_bed.py`, `ui/app_shell.py:3172-3206` | Reportado | Baja |

### 2.3 — Manejo ineficiente / frágil del estado

- **`StreamingSpeechPipeline.run()` sin error handling ni backpressure.** *Verificado* (`core/streaming_speech.py:16-19`): el bucle `for delta in self._llm.stream(...)` → `self._playback.speak(sentence)` no tiene `try/except` ni cola acotada. Cualquier excepción del stream o del playback propaga sin recuperación, y si el playback es lento las frases se acumulan en memoria. El archivo completo son 19 líneas (*verificado*), así que el "contrato mínimo" es literal. **Estado de consumo (verificado, ver §2.6): CÓDIGO MUERTO en producción** — la clase NO está importada ni referenciada en ningún módulo de runtime; solo la consume su test de contrato. Por eso la falta de error handling **no afecta el runtime actual**: la severidad efectiva en producción es baja hasta que se cablee. Si se decide cablearla, entonces sí es ruta de TTFA (time-to-first-audio) y la falta de `try/except`/backpressure pasa a severidad media.
- **Mutación de estado dispersa en 3 patrones** *(reportado, `app_shell.py`)*: `UIState.set()` vs. atributo directo vs. `command_queue.put()`. Sin patrón único. ROI: nice-to-have.
- **Dual failure counters en agenda** (§P3).

### 2.4 — Manejo de errores: supresión silenciosa (cross-cutting, ROI real)

Patrón recurrente `except Exception: pass`/`except: pass` sin log, que **oculta corrupción y errores de permisos**:

| Ubicación | Estado | Impacto |
|---|---|---|
| `config/settings.py` — 8 funciones save/load *(reportado: ~307,350,370,390,414,436,460,491)* | Reportado | Modelo guardado no carga sin diagnóstico |
| `ui/state.py:194-199` — dispatcher de observers traga toda excepción | Reportado | Callbacks de panel fallan en silencio |
| `core/health_monitor.py:78,100,146` — guards y watchdogs | Reportado | Degradación silenciosa del monitoreo |
| `stream_admin/oauth_store.py:75-76` — `_restrict_permissions` | Reportado | Token queda world-readable si `icacls` falla |

**Recomendación de ROI: ALTO.** Añadir `logger.warning/exception` antes de cada `pass` es barato y resuelve la queja #1 de debuggability en producción. Esto vale más que cualquier refactor estructural de las tablas anteriores.

### 2.5 — Traza verificada del TOCTOU sobre `_speaking` — por qué "Baja" es correcta

El correctivo pedía justificar la severidad. **Justificación basada en código leído:**

- **Quién limpia `_speaking` externamente y desde qué hilo.** *Verificado*: el único setter externo es `app_shell.py:1266` (`self.motor_ia._speaking = False`), dentro de `_kira_agenda_emergency_stop` (`app_shell.py:1244-1268`), que es un callback de UI (botón / `set_agenda_emergency_stop_callback`, `app_shell.py:907, 1085`) → corre en el **hilo principal de Tkinter**.
- **El write externo SÍ toma el mismo lock.** *Verificado*: `with self.motor_ia._lock: self.motor_ia._speaking = False` (`app_shell.py:1265-1266`). Las escrituras internas en el motor también están bajo `_lock` (`llm_engine.py:1486, 1492, 1530, 1839`).
- **El read del productor en `:1587` es sin lock**, pero es una lectura atómica de bool en CPython: ventana de lectura *stale*, no corrupción.
- **La ventana real es minúscula y el consumidor la cierra.** *Verificado*: el bucle del productor procesa **una oración por iteración** (`for i, oracion in enumerate(oraciones)`, `:1586`), y el **consumidor** (que reproduce el audio) re-chequea `_speaking` **bajo `_lock` en tres puntos**: antes de dequeue (`:1746-1748`), tras dequeue (`:1759-1766`) y dentro del busy-wait de pygame (`:1782-1784`, con `time.sleep(0.05)`), y además llama explícitamente `pygame.mixer.music.stop()` cuando detecta interrupción (`:1791-1793`).

**Conclusión:** aunque el read de `:1587` corra justo antes de que `emergency_stop` ponga `False`, el peor caso es que se **encole** una oración más; el consumidor la corta dentro de ~50 ms (granularidad del busy-wait) y para el mixer. El silencio efectivo se garantiza en 1-2 frames de audio, no se "continúa reproduciendo indefinidamente". Por eso **"Baja" es la severidad correcta**. Recomendación mínima opcional: leer `_speaking` bajo `_lock` también en `:1587` por consistencia, o documentar que `emergency_stop` garantiza silencio en ≤ 1-2 oraciones.

### 2.6 — Confirmación: `streaming_speech.py` es código muerto en producción (no contrato cableado)

El correctivo pedía resolver la ambigüedad. **Resuelto con evidencia de búsqueda en todo el repo:**

- *Verificado* (`grep` sobre `*.py`, `*.toml`, `*.cfg`, `*.spec`): las únicas referencias a `streaming_speech` / `StreamingSpeechPipeline` son su propio archivo (`opencohost/core/streaming_speech.py:8`) y su test (`tests/test_streaming_speech_pipeline.py:8,40,55,69,85`).
- *Verificado*: NO hay import en `pyproject.toml`/`setup.py`/`setup.cfg`/`.spec`, NO hay `__all__` que lo exporte, NO hay import dinámico (`importlib`/string) en código de runtime.
- *Verificado*: el test es de contrato puro, usa `FakeLLM`/`FakePlayback` (`tests/test_streaming_speech_pipeline.py:11-34`) y su docstring lo describe como "Contract tests for the streaming speech pipeline".

**Veredicto definitivo:** `StreamingSpeechPipeline` es **código muerto en producción, cubierto solo por tests de contrato** — no está cableado en el flujo de runtime. Es seguro (a) dejarlo documentado como contrato/stub no consumido, o (b) eliminarlo si no hay intención de cablearlo. No invertir en hardening (error handling/backpressure) hasta que exista un consumidor de producción real.

---

## 3. ROI por candidato — ¿problema real o "diseño bonito"?

### 3.1 — Vale la pena (problema real, fix acotado)

| Acción | Justificación | Riesgo de empaquetado |
|---|---|---|
| Timeout defensivo en `heavy_tts_block_reason()` (`llm_engine.py:1569`) | Defensa en profundidad (el riesgo de stall es bajo, §1.2); fix barato | Ninguno |
| `deque(maxlen=N)` para `rejection_log` (`kira_agenda_controller.py:385`) | Acota memoria en 24/7 (números en §3.4) | Ninguno |
| `maxsize` + manejo de `Full` en `command_queue` (`llm_engine.py:88`) | Evita OOM bajo ráfaga | Ninguno |
| Shutdown ordenado de daemons (join con timeout) | Cierre rápido, sin sockets huérfanos | Mejora cierre en .exe |
| Logging en lugar de `except: pass` (§2.4) | Debuggability en producción | Ninguno |
| Reset de `_character_repair_needed` al aceptar | Ahorro de tokens, logs claros | Ninguno |
| Rotación de logs (`logger.py`) | Evita llenar `%APPDATA%` en streams largos | Ninguno |
| Encapsular state machine de agenda (`AgendaOrchestrator`) | Centraliza P2+P3, mejora debug de freezes | Ninguno (extracción de lógica pura) |
| Eliminar código muerto RF4 (`stream_admin_ui.py`) | -20 KB de bundle, menos carga de revisión | **Mejora** bundle/empaquetado |

### 3.2 — NO vale la pena ahora (sobre-ingeniería, inestabilidad, o daño a empaquetado)

| Propuesta de los auditores | Por qué NO (todavía) |
|---|---|
| Partir las 4 God classes en 5-7 clases "limpias" de golpe | Refactor big-bang en fase de validación de runtime = riesgo de regresión > valor. Hacerlo **incremental y guiado por bugs**. |
| Adaptador/facade sobre `pygame.mixer` (`audio_bed.py`) | La inflación de bundle no está medida contra los imports reales de PyInstaller, así que se evita afirmarla como hecho. El punto que SÍ se sostiene: si `pygame.mixer` es el único backend de audio del roadmap, los adaptadores añaden costo de abstracción sin beneficio concreto. Mantener imports directos salvo que se planee genuinamente otro backend de audio. |
| Abstracción/interface sobre el cliente Ollama | Igual: capa sin segundo proveedor real. Riesgo de romper resolución de imports en el freeze. |
| `ProviderRegistry`/factory para stream providers | **Prematuro**: solo YouTube está completo; Twitch es placeholder. **Diferir el refactor a `ProviderRegistry` hasta que Twitch esté feature-complete o se apruebe un tercer proveedor.** Hoy es complejidad especulativa. |
| Reemplazar marshaling `_safe_after` por un event bus unificado | El patrón actual está **probado y funciona**. NO introducir un nuevo event-bus durante la fase de validación de runtime: el riesgo de bugs sutiles de ordenamiento de mensajes es mayor que el beneficio de "unificación". |
| Tratar la inversión daemon→UI como CRITICAL y rediseñar callbacks a colas | **Ya está marshalado** (§1.1). Esfuerzo desviado del defecto real. |
| i18n registry / desduplicar patrones regex entre módulos | Producto es Spanish-first hoy. Nice-to-have de higiene; no bloquea launch. |
| Guardrails componibles + cache de validación en agenda | Ahorro de CPU real pero micro; no es el cuello de botella. Posponer. |
| Mover `MODELS_CATALOG`/presets a YAML runtime | El restart es esperado en app empaquetada. ROI bajo. |
| Validación de transición con `threading.Lock` en el controlador de agenda | El controlador es determinista y (debería ser) main-thread-only. Documentarlo > añadir locks que no se necesitan. |

### 3.3 — Casos donde "arquitectura limpia" sería un DEFECTO aquí

- **Lazy-loading agresivo de paneles para "reducir el bundle":** la afirmación previa de que "CustomTkinter ya construye el árbol perezosamente en layout" **no es exacta y se corrige**: Tkinter/CustomTkinter instancian los widgets de forma *eager* al construirlos; lo que se difiere es el cálculo de geometría/render del gestor (`pack`/`grid`/`place`) hasta que la ventana se mapea. Lo que SÍ se sostiene con evidencia: forzar carga diferida manual añade estados de inicialización frágiles (los `if hasattr` actuales ya son un olor de esa fragilidad) y **no reduce significativamente el tamaño del `.exe`**, porque PyInstaller empaqueta los módulos igual, se instancien temprano o tarde. Riesgo > beneficio. (No se cita documentación de CustomTkinter porque no se verificó durante esta auditoría; la afirmación se acota a lo observable en el código del repo.)
- **Inyección de dependencias formal en `app_shell.__init__`:** convertir el wiring directo (`MotorVocalIA(...)`, `KiraAgendaController()`, …) en un contenedor DI agrega abstracción que PyInstaller debe rastrear y que dificulta el arranque. Para una app de ventana única local, el wiring directo es la elección correcta.
- **Separar `core/` en wheels independientes:** rompería los imports relativos de `settings.TEMP_DIR`/`logger` que hoy funcionan en el wheel, y multiplicaría la complejidad de empaquetado. El acoplamiento actual core↔config es tolerable.

### 3.4 — ROI cuantificado de la corrección de `rejection_log` (deque)

Números basados en la estructura real del registro (*verificado*, `kira_agenda_controller.py:897-905`): cada entrada es un `dict` pequeño con ~6 claves cortas (`error`, `guardrail`, `reason`, `length`, `state` + `extra`).

- **Memoria ahorrada por hora (estimación, no medida):** cada `dict` con strings cortos ronda ~250-400 bytes en CPython (overhead de dict + valores). Con una tasa típica de rechazo de ~1 por turno y ~2-4 turnos/min, son ~120-240 entradas/hora → **~30-100 KB/hora de crecimiento no acotado**. En un stream de 8 h: del orden de **cientos de KB a ~1 MB** sin liberar. Un `deque(maxlen=1000)` fija el techo en ~250-400 KB constantes, independiente de la duración del stream.
- **Costo de CPU de `get_metrics()` hoy:** *verificado* que itera la lista completa en cada llamada (`:922`), O(n) sobre todo el historial. Si un dashboard la sondea cada pocos segundos, el costo por llamada crece linealmente con la duración del stream (peor a las 8 h que al minuto 1). Con `deque(maxlen=1000)` el costo de `get_metrics()` queda acotado a O(1000) constante. Nota: la pérdida es que las métricas pasan a reflejar una **ventana** de los últimos N rechazos, no el total histórico; si se requiere el total acumulado, mantener contadores incrementales aparte (O(1) por rechazo) en vez de iterar.

---

## 4. Honestidad sobre incertidumbre

- Las severidades de `_generar_dialogo`, watchdog-thread-leak, dual counters, OllamaStartupManager y file handles de Qwen provienen de la síntesis de los 16 auditores; **confirmé la existencia del patrón pero no releí cada línea exacta**. Antes de actuar, validar `archivo:línea`.
- `StreamingSpeechPipeline` (`streaming_speech.py`) quedó **resuelto en esta revisión** (§2.6): es código muerto en producción, cubierto solo por tests de contrato. La ausencia de error handling es real (verificada) pero hoy sin impacto en runtime porque no está cableado.
- El riesgo del gate de salud P1 quedó **acotado en esta revisión** (§1.2): la llamada no hace I/O y el `HealthMonitor` tiene shutdown ordenado y timeouts en todo su polling. La afirmación previa de "stall si el HM se cuelga" estaba sobre-estimada.
- La predicción de tamaño del bundle PyInstaller (100-150 MB, +800 MB con heavy-tts) es estimación de los auditores, no medida. Tratar como orden de magnitud.
- La cota de memoria de `rejection_log` (§3.4) es estimación; el patrón de crecimiento sin eviction y el costo O(n) de `get_metrics()` sí están **verificados**.
