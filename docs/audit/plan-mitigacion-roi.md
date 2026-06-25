# Plan de Mitigación y ROI — Síntesis de los tres reportes de auditoría

| Campo | Valor |
|---|---|
| **Estado** | PLAN PARA APROBACIÓN — ningún cambio de código se ejecuta ahora |
| **Fecha** | 2026-06-16 |
| **Rama** | `audit/comprehensive-review` |
| **Alcance** | Paquete `opencohost/` (core, ui, smart_aggregator, stream_admin, avatar, config) |
| **Autor** | Arquitecto líder (síntesis de los tres ADR de auditoría) |
| **Insumos** | `adr-arquitectura-mantenibilidad.md`, `adr-deuda-tecnica-refactors.md`, `adr-framework-4r.md` |

> **Contrato de este documento.** Esto es un **plan de mitigación priorizado**, NO una implementación.
> NINGUNA mitigación se codifica aquí ni se aprueba aquí; cada ítem queda pendiente de decisión del
> owner. Las citas `archivo:línea` y las severidades provienen de los tres reportes fuente; donde un
> reporte ya **bajó** una severidad inflada de los 16 auditores, este plan **respeta la severidad
> corregida**, no la original. Las propuestas marcadas como **sobre-ingeniería** o **NO HACER** se
> nombran explícitamente para que no se conviertan accidentalmente en trabajo. El plan se cruza con los
> tracks abiertos del roadmap para **encajar, no duplicar**.
>
> **Huella de artefactos de esta planificación (declaración honesta).** No se modificó, creó ni borró
> ningún archivo de **código fuente** (`opencohost/`). Sin embargo, formalizar este plan en el roadmap
> SÍ tuvo una huella de artefactos de planificación: (1) se modificó `conductor/tracks.md` para registrar
> el track `branding_log_leak_remediation_20260616`; (2) se creó el directorio
> `conductor/tracks/branding_log_leak_remediation_20260616/`; (3) se creó
> `conductor/tracks/branding_log_leak_remediation_20260616/proposal.md`. Estos artefactos NO son código de
> producto, pero su existencia significa que la formalización de este plan **sí incluyó un paso de
> escritura en el registro del roadmap**. Ese track queda en estado **PROPOSAL ONLY** (ver `tracks.md`):
> propuesto, no iniciado ni aprobado para ejecución. La afirmación de "no se tocó nada" aplica
> estrictamente al **código fuente**, no a los artefactos de planificación recién enumerados.

---

## 0. Cómo leer este plan

**Niveles de prioridad:**

- **P0 — Gate de release.** Bloquea declarar readiness de OpenCohost. Concentra los CRÍTICOS de
  RELIABILITY/RESILIENCE que son el mismo cuello de botella visto desde tres ángulos.
- **P1 — Alto valor, bajo riesgo.** Fixes quirúrgicos acotados que mejoran estabilidad u observabilidad
  en streams largos (4–8 h / 24/7). No tocan arquitectura ni empaquetado.
- **P2 — Deuda real, no urgente.** Refactors de separación y limpiezas legítimas; se hacen
  **incrementales y guiados por bugs**, nunca big-bang en fase de validación.
- **P3 — Diferido / pendiente de decisión de roadmap.** Valor condicional a un evento futuro (2º
  proveedor LLM, cablear `StreamingSpeechPipeline`, catálogos de modelos de usuario).
- **NO HACER (over-engineering).** Propuestas que serían **DEFECTOS** en este contexto local-first /
  PyInstaller / thread-safe CTk. Se listan en §6 con su tradeoff.

**Lectura de la matriz (§7):**
- **Esfuerzo:** tiempo-persona aproximado (los reportes solo costearon explícitamente la descomposición
  de `VocalAIApp` en ~3–5 días; el resto son estimaciones de orden de magnitud, marcadas como tales).
- **Impacto:** beneficio runtime/operativo real, no estético.
- **Viabilidad packaging/UI:** si afecta negativamente Hatchling/PyInstaller o la frontera thread-safe
  de CustomTkinter (`_safe_after` / Observer de `UIState`). "Mejora" significa que **reduce** bundle o
  riesgo; "Negligible" significa cero impacto; "Riesgo" significa que toca el boundary de hilos.
- **Pragmatismo:** ¿resuelve un dolor real en producción o es puramente estético/teórico?

---

## 1. P0 — Gate de release (CRÍTICO, no se cierra readiness sin esto)

Los tres CRÍTICOS de confiabilidad de `adr-framework-4r.md` (REL-1, REL-5, RES-5) son **un único frente
de trabajo**: la falta de timeout/cancelación en el camino de inferencia pesada y su gate de salud. NO es
un refactor; son dos cambios quirúrgicos sobre `llm_engine.py` más una validación de runtime.

| ID | Mitigación | Origen | Evidencia |
|---|---|---|---|
| **P0-1** | **Validar contra modelo pesado real que se cuelgue** REL-1 + REL-5 + RES-5 antes de declarar readiness. | 4R (RS-5, gate de release); también `AGENT_HANDOFF.md` | `llm_engine.py:1174-1196` + `:1564-1585` — VERIFICADO en 4R |
| **P0-2** | **Timeout defensivo / fallback por defecto** alrededor de `heavy_tts_block_reason()` en el productor de TTS. | 4R (REL-5), Deuda (§1.2, §3.1) | `llm_engine.py:1569` (call-site), `:1564-1582` |
| **P0-3** | **Cancelación/marca del worker del watchdog de Ollama** para que el thread no quede huérfano en timeout. | 4R (REL-1), Deuda (watchdog leak) | `llm_engine.py:1174-1196` — VERIFICADO |

> **Matiz de severidad que NO debe perderse (Deuda §1.2).** El reporte de Deuda **bajó P0-2 de Alta a
> Media** como *defecto activo*: la llamada `heavy_tts_block_reason()` es side-effect-free y solo lee un
> snapshot bajo lock (`health_monitor.py:622`), y el `HealthMonitor` SÍ tiene shutdown ordenado
> (`stop()` + `join(timeout=5)`, `health_monitor.py:528-532`) con timeouts en todo su I/O. **NO hay un
> stall real a la espera de polling de red.** Aun así P0-2 se mantiene como gate porque es **defensa en
> profundidad barata** y porque P0-1 (validación contra cuelgue real) es la verdadera condición de
> readiness exigida por `AGENT_HANDOFF.md`. El valor de P0 está en la **validación** (P0-1) tanto como en
> el código (P0-2/P0-3). 4R sí lo clasifica como CRÍTICO desde el ángulo de resiliencia ante cuelgue;
> ambas lecturas conviven: el cuelgue es crítico de validar, el fix de gate es de severidad media.

**Cruce con roadmap (NO duplicar):** este P0 es exactamente el **gate de validación de runtime ya
pendiente** para `heavy_model_inference_recovery_20260609` (ver `CLAUDE.md` "Active local checkpoints" y
la roadmap "Runtime validation"). El plan **no abre un track nuevo**: P0-1 se ejecuta dentro de ese gate
existente; P0-2/P0-3 son los dos fixes que ese gate debe validar. Solapa parcialmente con
`qwen_tts_lifecycle_hardening_20260613` (auto-manage del subproceso Qwen, VRAM-gated), pero ese track es
sobre el **ciclo de vida del subproceso**, no sobre el timeout del gate dentro del productor — son
complementarios, no el mismo trabajo.

---

## 2. P1 — Alto valor, bajo riesgo (fixes acotados, sin tocar arquitectura)

Coinciden en los tres reportes como "SÍ vale la pena". Ninguno toca el boundary thread-safe ni el
empaquetado; varios lo **mejoran**.

| ID | Mitigación | Origen | Evidencia |
|---|---|---|---|
| **P1-1** | **Logging en lugar de `except: pass`** (cross-cutting): dispatch de `UIState`, `_safe_after`, init de HealthMonitor, save/load de `settings.py`, cleanup de paneles, `_restrict_permissions`. | Deuda (§2.4, ROI ALTO), 4R (rec. #3), Arq (#4) | `state.py:194-199`, `app_shell.py:2469-2472`, `settings.py` (~8 funcs), `oauth_store.py:75-76` |
| **P1-2** | **`deque(maxlen=N)` para `rejection_log`** — acota memoria y costo O(n) de `get_metrics()` en 24/7. | Deuda (§2.2, §3.4 cuantificado), Arq | `kira_agenda_controller.py:385,905,922` — VERIFICADO |
| **P1-3** | **`maxsize` + manejo de `Full`** en `command_queue` — evita OOM bajo ráfaga (PTT+WS+chat). | Deuda (§3.1), Arq (#2) | `llm_engine.py:88` — VERIFICADO (es `queue.Queue()` sin límite) |
| **P1-4** | **Shutdown ordenado de daemons** (join con timeout) en motor/prefetch/OBS retry/chat poller. Excepción: `HealthMonitor` ya lo tiene. | Deuda (§2.2, §3.1), 4R (RES-7, RES-2) | `llm_engine.py:176-179`, `app_shell.py:2753-2768` |
| **P1-5** | **Bufferizar/loggear eventos descartados por `_safe_after`** en la carrera de arranque (SPOF-3). | 4R (SPOF-3 VERIFICADO), Arq (§1.4), Deuda (§2.1) | `app_shell.py:2456-2472` — VERIFICADO |
| **P1-6** | **Cerrar TOCTOU de tokens OAuth**: crear el archivo con permisos restringidos *antes* de escribir y dejar de tragar la excepción de `_restrict_permissions`. | 4R (SEC-1/SEC-2, rec. #2) | `oauth_store.py:28-31` + `:55-76` — VERIFICADO |
| **P1-7** | **Reset de `_character_repair_needed` al aceptar** — ahorra tokens y limpia logs. | Deuda (§P3, §3.1) | `kira_agenda_controller.py` (701/810/824) — reportado |
| **P1-8** | **Rotación de logs** (`RotatingFileHandler`) — evita llenar `%APPDATA%` en streams largos. | Deuda (§2.2), 4R (SEC-5) | `config/logger.py:34-36` |
| **P1-9** | **Unificar `OBSConfig`** en una sola definición — elimina bug latente de divergencia de campos. | Arq (§3.4, #3), Deuda, 4R (DUP-3) | `obs_client.py:31` + `avatar_config.py:34` — VERIFICADO (idéntico) |
| **P1-10** | **Test de sincronización de `APP_ID`** entre `server_qwen.py` y `health_monitor.py:239`. | Arq (#5), 4R (DUP-5) | El drift ya causó un bug release-blocking (ver cruce abajo) |
| **P1-11** | **Cerrar/garantizar file handles de logs del subproceso Qwen** y terminar `OllamaStartupManager` en `cleanup()`. | Deuda (§2.2) | `health_monitor.py:250-265,301-302`, `model_panel.py:685-717` — reportado |

**Cruce con roadmap (NO duplicar):**

- **P1-10 (APP_ID):** el drift `voiceai-qwen-tts` vs `opencohost-qwen-tts` **YA fue arreglado** y mergeado
  vía PR #46 (`qwen_tts_lifecycle_hardening_20260613`, engram #1931). Por lo tanto P1-10 NO es "arreglar
  el drift" — es **añadir el test de sincronización** que evita que reaparezca. Encaja como tarea de
  hardening dentro de ese track o de `repo_hygiene_audit_20260612`.
- **P1-1 (logging) / P1-8 (rotación de logs):** parte del subsistema de logger lo tocaría
  `branding_log_leak_remediation_20260616` (renombrar `getLogger("VoiceAI")` en `logger.py:43`,
  `avatar_panel.py:36`, `obs_client.py:25` **juntos** para no perder el `SensitiveDataFilter`). P1-1/P1-8
  deben **coordinarse** con ese track para tocar `config/logger.py` una sola vez y no romper la redacción
  de tokens/liveChatId. NO hacerlo en paralelo a ciegas.
  **Nota de procedencia del track (importante, lectura honesta):** a diferencia de los demás tracks
  cruzados en §2/§3 —que son tracks **preexistentes** del roadmap—,
  `branding_log_leak_remediation_20260616` **fue creado como parte de formalizar esta planificación** (su
  entrada en `conductor/tracks.md` y su `proposal.md` no existían antes de redactar este plan; ver la
  "Huella de artefactos" en el contrato). Queda en estado **PROPOSAL ONLY**: propuesto y registrado, no
  iniciado ni aprobado para ejecución. Por lo tanto, las referencias cruzadas a este track NO describen un
  track ya activo, sino uno **recién propuesto** durante esta síntesis; el resto de tracks citados
  (`heavy_model_inference_recovery_20260609`, `qwen_tts_lifecycle_hardening_20260613`,
  `repo_hygiene_audit_20260612`, `stream_admin_legacy_removal_20260614`,
  `viewer_queue_backpressure_20260613`, `kira_history_summarization_20260611`,
  `ui_rendering_optimization_20260609`) sí son descubrimientos de tracks preexistentes del roadmap.
- **P1-3 (command_queue maxsize):** relacionado conceptualmente con
  `viewer_queue_backpressure_20260613` (cola de viewer-query acotada), pero son colas distintas
  (`command_queue` del motor vs cola de consultas de viewers). NO fusionarlos; sí compartir el patrón de
  backpressure. Además ese track está **bloqueado** por prerequisitos en
  `kira_history_summarization_20260611` (runaway-cap + lane de operador priority=0), así que P1-3 NO debe
  esperar a ese track: es independiente y se puede hacer antes.

---

## 3. P2 — Deuda real, no urgente (incremental, guiado por bugs)

Los tres reportes coinciden: la deuda estructural es **real** pero un refactor big-bang en plena fase de
validación de runtime **introduce más riesgo de regresión del que resuelve**. Se hace por extracción
incremental, con auditoría de hilos obligatoria, NO de golpe.

| ID | Mitigación | Origen | Evidencia |
|---|---|---|---|
| **P2-1** | **Extraer `AgendaOrchestrator`** (lógica sin Tk: tick loop, prefetch, consumo de chat, transiciones de estado). Resuelve P2+P3 de Deuda a la vez y la mutación externa del state machine. | Deuda (§P2/§P3, ROI top de agenda), 4R (rec. #4), Arq (§3.5) | `app_shell.py:1367` (mutación externa) + `kira_agenda_controller.py` |
| **P2-2** | **Partir `_build_ui()` (~570 LOC) en sub-factories** por grupo de paneles (header/main/footer). Por **legibilidad/revisabilidad estática**, NO por hot-reload. | Arq (§4, ✅ hacer), Deuda (§P2, ROI bajo pero válido), 4R (RD-1) | `app_shell.py:413-985` — VERIFICADO ~570 LOC |
| **P2-3** | **Eliminar código muerto RF4** detrás de `STREAM_ADMIN_ENABLED=False` (~200 LOC). **Mejora** el bundle. | Deuda (§3.1, mejora empaquetado), Arq (anti-patrón), 4R | `stream_admin_ui.py:365,438,493` |
| **P2-4** | **Descomponer `VocalAIApp` en coordinadores** (`MotorCoordinator`/`StreamAdminCoordinator`/`UIBuilder`) — refactor de **separación**, no de abstracción. Único refactor explícitamente costeado: ~3–5 días-persona. | Arq (§3.5, costeada), Deuda (P2, post-validación), 4R (rec. #4 acotado) | `app_shell.py` 3256 LOC, 171 métodos — VERIFICADO |
| **P2-5** | **Centralizar guard de transición del state machine de agenda** (eliminar mutación externa desde UI). Acompaña a P2-1. | Arq (§2, state machine disperso), Deuda (§P3) | `kira_agenda_controller.py` (3 métodos) + `app_shell.py:1367` |
| **P2-6** | **`TypedDict`/dataclass de widgets** para `StreamAdminUI._widget(name)` (113+ lookups por string). | Deuda (§P4), 4R (RD-3) | `stream_admin_ui.py` — reportado |
| **P2-7** | **Desduplicar `get_app_dir`/resolución de paths** entre `settings.py` y `storage.py`. | Arq (§3.4), 4R (DUP-6) | `settings.py` + `storage.py` |
| **P2-8** | **Alias por `@property` para los dual failure counters** de agenda (`failure_count` vs `recovery.failure_count`). | Deuda (§P3, nice-to-have), 4R (DUP-4) | `kira_agenda_controller.py` (370/808-809) |

> **Restricción NO NEGOCIABLE para P2-1, P2-4, P2-5 (Arq §3.5, 4R RD-1, Deuda §P1).** Cualquier
> descomposición de `VocalAIApp` / `MotorVocalIA` exige **auditoría de hilos como parte no-opcional del
> trabajo** y re-ejecución de la suite de recovery/health antes de mergear. El boundary `_safe_after` /
> `_on_motor_event` y el Observer de `UIState` son **el activo más valioso del repo**; fragmentar mal el
> coordinador *introduce* bugs de thread-safety. Regla de 4R (RD-1, rec. #4): **extraer solo lógica de
> negocio SIN Tk** y **dejar el wiring de widgets + el dispatch `_safe_after` en una sola clase** que sea
> el único dueño del boundary de hilo. No es elegante, es seguro.

**Cruce con roadmap (NO duplicar):**

- **P2-3 (RF4 dead code):** está explícitamente **diferido** a
  `stream_admin_legacy_removal_20260614` (NO-PRIORITY), que requiere (1) dep audit completo,
  (2) `youtube_chat_compliance_audit_20260614` resuelto, (3) autorización del owner para borrar. Los
  paneles RF4 ya están **HIDDEN** (no borrados) vía `STREAM_ADMIN_ENABLED=False` (mergeado en
  `opencohost_ui_declutter_20260614`, PR #46). Por lo tanto P2-3 **NO se ejecuta ahora**: es la fase final
  de ese track NO-PRIORITY, no una tarea nueva del plan.
- **P2-2 (sub-factories de `_build_ui`):** encaja naturalmente dentro de `UIBuilder` de P2-4 y se
  relaciona con `ui_rendering_optimization_20260609` (rama `audit/ui-rendering-analysis`, ADR-006/007).
  Si ese track va a tocar `_build_ui` por razones de render/layout, P2-2 debería **piggyback** ahí para no
  tocar el método dos veces. Confirmar con el owner de ese track antes de programar P2-2 por separado.
- **P2-1/P2-5 (AgendaOrchestrator + guard de transición):** se relacionan con
  `kira_history_summarization_20260611` (memoria L1 ya implementada, awaiting runtime gate) solo en que
  ambos tocan el flujo de agenda/pipeline; NO son el mismo trabajo (memoria vs coordinación de
  turn-taking). Programar P2-1 **después** del gate de runtime de ese track para no mover dos pisos a la
  vez.
- **P2-6/P2-7/P2-8 (lookups por string, paths duplicados, dual counters):** son exactamente el tipo de
  "consolidated low/med cleanup" que ya tiene track propio en `repo_hygiene_audit_20260612` (PROPOSAL
  ONLY: dead code, filter pattern duplication, stale path refs). **Deben absorberse en ese track**, no
  abrirse como trabajo separado.

---

## 4. P3 — Diferido / pendiente de decisión de roadmap (valor condicional)

No son over-engineering definitivo, pero su ROI **depende de un evento futuro**. Quedan documentados como
diferidos, NO rechazados permanentemente.

| ID | Mitigación | Origen | Condición que lo activaría |
|---|---|---|---|
| **P3-1** | **`MODELS_CATALOG` desde YAML en runtime** (hoy dict en `settings.py`). | Arq (§4 ⚠️), Deuda (§3.2) | Si el roadmap añade catálogos de modelos provistos por el usuario. Hoy un usuario no puede añadir un modelo local sin editar `settings.py` y rebuildear — limitación real pero tolerable. |
| **P3-2** | **Hardening de `StreamingSpeechPipeline`** (error handling + backpressure). | Deuda (§2.3/§2.6 VERIFICADO), 4R (RES-11) | Solo si se **cablea** a producción. Hoy es **código muerto** (solo lo consume su test de contrato) — invertir ahora sería hardening de código no usado. |
| **P3-3** | **`ProviderRegistry`/factory para stream providers.** | Deuda (§3.2 prematuro), Arq (§2 ⚠️ sin factory) | Cuando Twitch esté feature-complete o se apruebe un 3er proveedor. Hoy solo YouTube está completo, Twitch es placeholder. |
| **P3-4** | **`ProfileStore` con DI de I/O para testabilidad.** | Arq (§4 ⚠️ diferir) | Solo si surge necesidad concreta de testear el I/O de perfiles aislado. Hoy el boilerplate supera el beneficio. |
| **P3-5** | **Medir baseline de arranque antes de decidir lazy-load de paneles.** | Arq (§4 ⚠️ medir primero) | Acción correcta = **instrumentar** arranque/carga de paneles. Sin baseline, "vale la pena" o "marginal" es especulación. NO es adoptar lazy-load; es medir para poder decidir. |
| **P3-6** | **Guardrails componibles + cache de validación** en agenda. | Deuda (§3.2 posponer), Arq (§2) | Ahorro de CPU real pero micro; no es el cuello de botella. Posponer hasta que el perfil de CPU lo justifique. |

---

## 5. Defectos puntuales de severidad BAJA / corregida (no entran a P0–P2 como urgentes)

Los reportes ya **bajaron** estas severidades respecto de la afirmación original de los 16 auditores.
Quedan documentados para no reabrirlos como críticos por error.

| Ítem | Severidad corregida | Por qué NO es urgente | Fuente |
|---|---|---|---|
| Inversión callback daemon→UI (motor `ui_callback`) | **MEDIA, no CRÍTICA** | Ya está marshalado: `_on_motor_event` chequea hilo y enruta a `_safe_after` (`app_shell.py:2474-2478`). Acción correcta = **documentar el contrato** + assert de thread en debug, NO reescribir el sistema de eventos. | Arq (§1.3), Deuda (§1.1), 4R (REL-10) |
| TOCTOU sobre `_speaking` | **BAJA (justificada)** | El consumidor re-chequea bajo lock en 3 puntos; silencio garantizado en ~50 ms. Opcional: leer bajo lock por consistencia. | Deuda (§2.5 VERIFICADO), 4R (RS-8) |
| `SensitiveDataFilter` como DoS por regex backtracking | **DESCARTADO** | Los 6 patrones usan clases negadas sin cuantificadores anidados — la forma que NO dispara backtracking. El filtro es correcto, **conservarlo**. | 4R (R-2 nota) |
| `_build_ui` "no es deuda porque Tkinter es lazy" | **Corregido: SÍ es deuda de legibilidad** | Tkinter instancia widgets *eager*; lo lazy es solo el cálculo de geometría. El argumento de hot-reload es irrelevante. Tratado como P2-2 (legibilidad estática). | Arq (§4), Deuda (§3.3) |

---

## 6. NO HACER — over-engineering / DEFECTOS en este contexto

El prompt exige nombrar explícitamente lo que sería sobre-ingeniería. Los tres reportes coinciden: estas
propuestas serían **DEFECTOS** para una app local-first empaquetada con PyInstaller con frontera
thread-safe de CTk. NO se programan en ningún P.

| Propuesta rechazada | Por qué es un DEFECTO aquí | Fuente |
|---|---|---|
| **Bus de eventos genérico** que reemplace `_safe_after` + `CallbackDispatcher` + dict dispatch | **Oculta** la frontera de hilo, que hoy es explícita y verificable. Riesgo de correctness alto sobre el activo más valioso del repo. Cero ganancia runtime. | Arq (§4), Deuda (§3.2), 4R (PRAGMATISMO RL/RD) |
| **Capa de abstracción/puertos sobre Ollama** (`LLMProvider`) | YAGNI: superficie de código sin un 2º backend real. No elimina el SPOF (sigue habiendo un solo Ollama local). Riesgo de romper resolución de imports en el freeze. | Arq (§4), Deuda (§3.2), 4R (R-3 PRAGMATISMO) |
| **Adapter/facade sobre `pygame.mixer`** | Bytes muertos en el `.exe` para un backend de audio hipotético que nunca se empaqueta. | Arq (§4), Deuda (§3.2), 4R |
| **Inyección de dependencias formal (DI container) en `app_shell.__init__`** | Abstracción que PyInstaller debe rastrear y que dificulta el arranque. Para una app de ventana única, el wiring directo es correcto. | Deuda (§3.3) |
| **Separar `core/` en wheels independientes** | Rompería imports relativos (`settings.TEMP_DIR`/`logger`) y multiplicaría complejidad de empaquetado. | Deuda (§3.3) |
| **Lazy-load agresivo de paneles "para reducir bundle"** | PyInstaller empaqueta los módulos igual; no reduce el `.exe`. Añade estados de init frágiles y riesgo de romper el wiring. (Medir primero = P3-5, NO adoptar a ciegas.) | Arq (§4), Deuda (§3.3) |
| **Partir las 4 God classes en 5–7 clases "limpias" de golpe (big-bang)** | Refactor masivo en fase de validación = riesgo de regresión > valor. Hacerlo incremental (P2-1/P2-4), guiado por bugs. | Deuda (§3.2), 4R, Arq (§3.5) |
| **Tratar la inversión daemon→UI como CRITICAL y rediseñar callbacks a colas** | Ya está marshalado (§5). Duplicaría el mecanismo. Esfuerzo desviado del defecto real. | Arq (§1.3), Deuda (§3.2), 4R |
| **i18n registry / desduplicar regex de chat entre módulos por "limpieza"** | Producto es Spanish-first hoy; nice-to-have de higiene, no bloquea launch. (La desduplicación de patrones de filtro sí está en `repo_hygiene_audit_20260612` si se decide.) | Deuda (§3.2), 4R (DUP-1) |

---

## 7. Matriz consolidada Item × (Origen · Esfuerzo · Impacto · Viabilidad · Pragmatismo · Recomendación)

> Leyenda Esfuerzo: XS (<½ día) · S (~1 día) · M (~2–3 días) · L (~3–5 días, único costeado en los
> reportes). Las estimaciones distintas a P2-4 son orden de magnitud (los reportes no las costearon
> línea a línea). Viabilidad: **Mejora** / **Negligible** / **Riesgo (boundary de hilos)**.

| Item | Origen (reporte) | Esfuerzo | Impacto | Viabilidad packaging/UI | Pragmatismo | Recomendación |
|---|---|---|---|---|---|---|
| **P0-1** Validar cuelgue de modelo pesado real | 4R, Deuda, Arq | M (sesión de validación runtime) | **Muy alto** — gate de release | Negligible (no toca código) | Dolor real en vivo: TTS enmudece si el path pesado se atasca | **HACER PRIMERO — gate** |
| **P0-2** Timeout defensivo en gate de salud | 4R(REL-5), Deuda(§1.2) | XS | Alto (defensa en profundidad) | Negligible | Real pero acotado (no hace I/O); cinturón-y-tirantes | **HACER (dentro del gate P0-1)** |
| **P0-3** Cancelación del worker watchdog | 4R(REL-1), Deuda | S | Alto (fuga de threads en 24/7) | Negligible | Dolor real en streams largos | **HACER (dentro del gate P0-1)** |
| **P1-1** Logging en lugar de `except: pass` | Deuda(§2.4), 4R, Arq | S | **Alto** (queja #1 de debuggability) | Negligible | Puro dolor de producción | **HACER** |
| **P1-2** `deque(maxlen)` en `rejection_log` | Deuda(§3.4), Arq | XS | Medio-alto (memoria + O(n) en 24/7) | Negligible | Real, cuantificado | **HACER** |
| **P1-3** `maxsize`+`Full` en `command_queue` | Deuda, Arq | XS | Medio (OOM bajo ráfaga) | Negligible | Real en flood PTT+WS+chat | **HACER** |
| **P1-4** Shutdown ordenado de daemons | Deuda, 4R | M | Medio (cierre limpio, sin sockets huérfanos) | **Mejora** cierre del `.exe` | Real en empaquetado | **HACER** |
| **P1-5** Buffer/log de eventos de `_safe_after` | 4R(SPOF-3), Arq, Deuda | S | Medio (no perder estado de arranque) | Negligible | Real (pérdida silenciosa de "Ollama caído") | **HACER** |
| **P1-6** Cerrar TOCTOU OAuth | 4R(SEC-1/2) | S | Alto (token world-readable en multiusuario) | Negligible | Real de seguridad local | **HACER** |
| **P1-7** Reset de `_character_repair_needed` | Deuda(§P3) | XS | Bajo-medio (tokens + logs) | Negligible | Real, trivial | **HACER** |
| **P1-8** Rotación de logs | Deuda, 4R(SEC-5) | XS | Medio (llenado de `%APPDATA%`) | Negligible | Real en 24/7 | **HACER (coord. branding track)** |
| **P1-9** Unificar `OBSConfig` | Arq, Deuda, 4R(DUP-3) | XS | Medio (bug latente de divergencia) | Negligible | Real | **HACER** |
| **P1-10** Test de sync de `APP_ID` | Arq, 4R(DUP-5) | XS | Medio (drift ya causó bug release-blocking) | Negligible | Real (preventivo) | **HACER (test; el fix ya está mergeado)** |
| **P1-11** Cerrar file handles Qwen / kill OllamaStartupManager | Deuda(§2.2) | S | Medio (zombies/handles en 4–8 h) | **Mejora** runtime | Real en streams largos | **HACER** |
| **P2-1** Extraer `AgendaOrchestrator` | Deuda, 4R(#4), Arq | M | Medio-alto (debug de freezes, testeable sin Tk) | **Riesgo** (boundary hilos) — mitigar con auditoría | Real (debuggability) | **HACER (post-gate, incremental)** |
| **P2-2** Sub-factories de `_build_ui` | Arq(§4), Deuda, 4R | S | Medio (legibilidad/revisabilidad) | Negligible (corte mecánico) | Real (revisión de 570 LOC) | **HACER (piggyback UI track)** |
| **P2-3** Eliminar dead code RF4 | Deuda, Arq, 4R | S | Medio (~20 KB bundle + carga de revisión) | **Mejora** bundle | Real pero modesto | **DIFERIDO al track legacy NO-PRIORITY** |
| **P2-4** Descomponer `VocalAIApp` en coordinadores | Arq(§3.5), Deuda, 4R | **L (~3–5 días, único costeado)** | Alto a largo plazo; bajo a corto | **Riesgo (boundary hilos)** — auditoría obligatoria | Real (frena crecimiento super-lineal) | **HACER incremental, post-validación; NO big-bang** |
| **P2-5** Guard de transición central de agenda | Arq(§2), Deuda(§P3) | S | Medio (encapsulación + debug) | Negligible | Real | **HACER (junto a P2-1)** |
| **P2-6** Widgets tipados en `StreamAdminUI` | Deuda(§P4), 4R | S | Medio (hazard de refactor) | Negligible | Real como hazard | **DIFERIDO a repo_hygiene track** |
| **P2-7** Desduplicar `get_app_dir`/paths | Arq, 4R(DUP-6) | XS | Bajo-medio (DRY, frágil en wheels) | Negligible (puede tocar paths PyInstaller — revisar) | Real | **DIFERIDO a repo_hygiene track** |
| **P2-8** `@property` para dual failure counters | Deuda(§P3), 4R | XS | Bajo (divergencia silenciosa) | Negligible | Nice-to-have | **DIFERIDO a repo_hygiene track** |
| **P3-1** `MODELS_CATALOG` a YAML runtime | Arq, Deuda | M | Condicional (extensibilidad de usuario) | **Riesgo** (path no empaquetado) | Teórico hoy, real si hay roadmap de extensión | **DIFERIR (decisión de roadmap)** |
| **P3-2** Hardening `StreamingSpeechPipeline` | Deuda(§2.6), 4R | S | Cero hoy (código muerto) | Negligible | Estético hoy (no cableado) | **DIFERIR (solo si se cablea)** |
| **P3-3** `ProviderRegistry` de stream | Deuda, Arq | M | Condicional (2º proveedor) | Negligible | Prematuro (Twitch placeholder) | **DIFERIR** |
| **P3-4** `ProfileStore` con DI de I/O | Arq(§4) | S | Bajo | Negligible | Teórico hoy | **DIFERIR** |
| **P3-5** Medir baseline de arranque | Arq(§4) | S | Medio (habilita decisión informada) | Negligible | Real (sin datos no se decide) | **HACER la MEDICIÓN; NO el lazy-load** |
| **P3-6** Guardrails componibles + cache | Deuda, Arq | M | Micro (CPU, no es cuello de botella) | Negligible | Teórico hoy | **DIFERIR** |
| **NH-1** Bus de eventos genérico | Arq, Deuda, 4R | — | Negativo | **Riesgo (oculta boundary)** | Over-engineering | **NO HACER** |
| **NH-2** Abstracción de proveedor Ollama | Arq, Deuda, 4R | — | Negativo (YAGNI) | Riesgo (imports en freeze) | Over-engineering | **NO HACER** |
| **NH-3** Adapter sobre `pygame.mixer` | Arq, Deuda, 4R | — | Negativo (bloat) | **Riesgo (bundle)** | Over-engineering | **NO HACER** |
| **NH-4** DI container en `app_shell` | Deuda | — | Negativo | **Riesgo (freeze/arranque)** | Over-engineering | **NO HACER** |
| **NH-5** `core/` en wheels separados | Deuda | — | Negativo | **Riesgo (empaquetado)** | Over-engineering | **NO HACER** |
| **NH-6** Lazy-load agresivo de paneles | Arq, Deuda | — | Negativo / nulo | **Riesgo (wiring)** | Over-engineering | **NO HACER (medir = P3-5)** |
| **NH-7** Big-bang de las 4 God classes | Deuda, 4R, Arq | — | Negativo en fase de validación | **Riesgo (regresión)** | Over-engineering de timing | **NO HACER (usar P2 incremental)** |

---

## 8. Secuencia recomendada (resumen ejecutivo del plan)

1. **P0 primero — gate de release.** P0-1 (validación de cuelgue real) + P0-2/P0-3 (los dos fixes que esa
   validación debe ejercitar). Sin esto no se declara readiness. Vive dentro del gate ya pendiente de
   `heavy_model_inference_recovery_20260609`.
2. **P1 en paralelo donde sea seguro.** Fixes XS/S de observabilidad, memoria y seguridad. Coordinar
   P1-1/P1-8 con `branding_log_leak_remediation_20260616` (mismo `logger.py`). P1-10 es solo el test
   (el fix de APP_ID ya está mergeado).
3. **P2 después del gate, incremental.** Empezar por `AgendaOrchestrator` (P2-1/P2-5) y sub-factories
   (P2-2). La descomposición de `VocalAIApp` (P2-4) **solo post-validación**, con auditoría de hilos
   obligatoria, jamás big-bang. Absorber P2-6/P2-7/P2-8 en `repo_hygiene_audit_20260612` y P2-3 en
   `stream_admin_legacy_removal_20260614`.
4. **P3 cuando el roadmap lo active.** Documentados como diferidos, no rechazados. P3-5 (medir arranque)
   es la única acción "ahora", y es **medición**, no implementación.
5. **NO HACER — congelado.** Las 7 propuestas de §6 no entran a ningún P; cualquiera que reaparezca como
   trabajo debe re-justificarse contra su tradeoff documentado.

> **Recordatorio final del contrato.** Ninguna mitigación de este plan se ha codificado ni aprobado;
> no se modificó, creó ni borró ningún archivo de **código fuente** (`opencohost/`). La única huella de
> escritura fue de **artefactos de planificación** del roadmap: `conductor/tracks.md` (modificado para
> registrar el track de branding) y el directorio/`proposal.md` de
> `branding_log_leak_remediation_20260616` (creados), todo en estado **PROPOSAL ONLY**. Cada P queda
> pendiente de decisión del owner. El plan está diseñado para **encajar con el roadmap existente** (no
> duplicar tracks) y para **respetar la regla de pragmatismo**: estabilidad, peso del bundle y
> thread-safety de la UI mandan sobre la elegancia arquitectónica.

---

*Fin del plan de mitigación. Documento solo de planificación; no se modificó, creó ni borró código
fuente (`opencohost/`). La única huella de escritura fue de artefactos de planificación del roadmap
(`conductor/tracks.md` + el directorio/`proposal.md` del track `branding_log_leak_remediation_20260616`,
en estado PROPOSAL ONLY), detallada en la "Huella de artefactos" del contrato de apertura.*
