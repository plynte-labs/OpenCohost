# ADR de Auditoría — Bloque 1: Arquitectura y Mantenibilidad

| Campo | Valor |
|---|---|
| **Estado** | DIAGNÓSTICO — solo análisis, sin cambios de código |
| **Fecha** | 2026-06-16 |
| **Rama** | `audit/comprehensive-review` |
| **Alcance** | Paquete `opencohost/` (UI, core, smart_aggregator, config, stream_admin, avatar, entrypoints) |
| **Autor** | Arquitecto líder (síntesis de 16 auditores especialistas) |
| **Tipo** | Informe técnico de arquitectura y mantenibilidad |

> **Nota metodológica.** Cada afirmación no trivial cita `archivo:línea`. Los tamaños de archivo y las
> rutas fueron verificados directamente contra el código (`wc -l`, lectura puntual). Cuando un hallazgo
> de los auditores resultó ser una **inferencia** o una **exageración**, lo marco explícitamente y aporto
> la evidencia correctiva. No tomo las conclusiones de los auditores como hechos sin verificarlas.

---

## Resumen ejecutivo

OpenCohost **no implementa una arquitectura formal pura** (ni hexagonal, ni MVC, ni MVP). Lo que existe
es una **arquitectura en capas pragmática y orientada a eventos**, con tres capas reales:

1. **Presentación / Wiring** (`opencohost/ui/`) — CustomTkinter, con `VocalAIApp` (`app_shell.py`) como
   raíz que construye, cablea y orquesta TODO.
2. **Lógica de negocio** (`opencohost/core/`, `opencohost/smart_aggregator/`, `opencohost/stream_admin/`,
   `opencohost/avatar/`) — motor LLM, pipeline TTS, agenda, agregador de chat, admin de stream, avatar.
3. **Configuración y persistencia** (`opencohost/config/`, stores SQLite en `core/`).

El **patrón estructural más fuerte y mejor ejecutado** es el **Observer thread-safe** de `UIState`
(`ui/state.py`), que mantiene un contrato de seguridad de hilos correcto entre los hilos de fondo
(motor, health, OBS, chat) y el bucle único de Tkinter. Este es el activo arquitectónico que **NO debe
tocarse a la ligera**: es la frontera que mantiene la app estable.

El **problema central de mantenibilidad** es la concentración de responsabilidades en pocas clases God
Object, encabezadas por `VocalAIApp` (**3256 líneas verificadas**, 171 métodos) y `MotorVocalIA`
(**1848 líneas verificadas**, **57 métodos** que abarcan **al menos 8 concerns distintos** — ver §3.1).
Son mantenibles HOY por ingenieros que las conocen, pero están **al borde de la insostenibilidad**: cada
cambio multi-hilo obliga a auditar todo el archivo.

**Veredicto de mantenibilidad a largo plazo: CONDICIONAL.** La base es funcional y bien testeada. La
suite enfocada citada en `CLAUDE.md` (`test_llm_tiers`, `test_model_panel`,
`test_heavy_model_inference_recovery`) arroja **97 tests pasando** (verificado en esta auditoría); la
suite completa colecta **2396 tests** (verificado con `pytest --collect-only`). La cifra "154–159" que
circula en notas previas **no coincide con ninguna de las dos** y se trata como **dato obsoleto** (ver
§3.5 para la estrategia y cobertura de tests). La disciplina de fronteras `core`→`ui` es razonable (el
core no importa CustomTkinter). Pero la deuda de complejidad de `app_shell.py` y `llm_engine.py`, sumada
al acoplamiento de configuración por constantes a nivel de módulo, hará que el costo de cambio crezca
super-linealmente si no se descomponen esas dos clases.

**Advertencia de pragmatismo (regla del prompt).** Varias "mejoras de arquitectura limpia" propuestas
por los auditores serían **DEFECTOS** en este contexto local-first/PyInstaller, no mejoras. Las nombro
explícitamente en §4 **con su tradeoff razonado (costo vs. beneficio)**. Refactorizar por elegancia,
introducir buses de eventos genéricos, capas de abstracción sobre Ollama, o adaptadores sobre pygame,
**aumentaría el peso del bundle y/o el riesgo de romper la thread-safety de la UI sin beneficio runtime**.

---

## 1. ¿Qué arquitectura de diseño general usa el proyecto?

### 1.1 Diagnóstico: capas pragmáticas + orientación a eventos (NO hexagonal, NO MVC/MVP)

No hay puertos/adaptadores, ni inversión de dependencias formal, ni separación
modelo-vista-controlador. Lo que hay es:

- **Separación física por capas** mediante paquetes (`ui/`, `core/`, `config/`, etc.).
- **Comunicación orientada a eventos** mediante callbacks y un Observer (`UIState`).
- **Wiring centralizado imperativo** en `VocalAIApp.__init__` y `_build_ui`.

La frontera **más respetada** es `core` → `ui`: los módulos de `core/` **no importan CustomTkinter**;
notifican a la UI vía un callable `ui_callback` inyectado en construcción
(`llm_engine.py:87 self.ui_callback = ui_callback`). Esto es disciplina arquitectónica real y correcta.

La frontera **más violada** es la inversa: `ui` → `core`. `app_shell.py` instancia y manipula
directamente lógica de negocio:

```
app_shell.py:190  self.motor_ia = MotorVocalIA(self.log_queue, self._on_motor_event)
app_shell.py:149  self.editorial_cards = EditorialCardStore(EDITORIAL_CARDS_DB)
app_shell.py:214  self._agenda_persistence = AgendaPersistence(EDITORIAL_CARDS_DB, ...)
```

`VocalAIApp` no es una capa de composición delgada: **posee y manipula estado de negocio** (motor,
agenda, audio bed, admin de stream). Esto es la causa raíz del God Object (ver §3).

> **CONTRADICCIÓN IMPORTANTE (docstring de aspiración vs. realidad).** El docstring de
> `app_shell.py:1-9` afirma que `VocalAIApp` es **únicamente** responsable de: "Creating UIState and all
> panel instances, Wiring callbacks, Calling panel.build() methods, Setting up window geometry,
> Delegating cleanup to all panels" y remata con "*No UI construction code is inline — all delegated to
> panels.*" **La realidad lo contradice y verificable en código:** `VocalAIApp` posee y manipula
> directamente estado de negocio (`editorial_cards` en `:149`, `audio_bed` en `:148`, `motor_ia` en
> `:190`, `kira_agenda` en `:191`, `health_monitor` en `:203`, `_agenda_persistence` en `:214`, además
> de `admin_manager` y `obs_client`), y **sí contiene construcción de UI inline**: `_build_ui()`
> (`:413`) ocupa ~570 líneas de definición de widgets dentro de la misma clase. El docstring describe el
> **diseño pretendido**, no el **diseño real**. Esta brecha entre aspiración y realidad **es la causa
> raíz del problema God Object** descrito en §3.5; documentarla como tal evita que un lector nuevo tome
> el docstring por una descripción fiel del estado actual.

### 1.2 Límites de cada subsistema y cómo se comunican

**Diagrama de capas y dependencias (vista de alto nivel):**

```
+----------------------------------------------------------------------+
|  CAPA 1 — PRESENTACIÓN / WIRING   (opencohost/ui/)                    |
|                                                                      |
|   VocalAIApp (app_shell.py)  ── construye, cablea y orquesta TODO    |
|     |  posee: motor_ia, kira_agenda, audio_bed, health_monitor,      |
|     |         editorial_cards, _agenda_persistence, admin_mgr, obs   |
|     |                                                                |
|   paneles (model, agenda, stream_admin, avatar, music, ...)          |
|   UIState (state.py)  ── Observer thread-safe (event loop único Tk)  |
+----------------------------------------------------------------------+
        |  llama métodos de core directamente (ui -> core)            ^
        |  command_queue.put(...) + ui_callback inyectado             |
        v                                                              |
        |   eventos marshalados al hilo principal vía _safe_after ----+
+----------------------------------------------------------------------+
|  CAPA 2 — LÓGICA DE NEGOCIO                                          |
|    core/ (llm_engine, health_monitor, audio_bed, stores)            |
|    smart_aggregator/ (aggregator, kira_agenda_controller)           |
|    stream_admin/ (admin_manager, providers)                         |
|    avatar/ (obs_client, avatar_state)                               |
|                                                                      |
|    REGLA RESPETADA: core/ NO importa ui/ (✅ aislado)                |
+----------------------------------------------------------------------+
        |  imports directos de constantes (alto fan-out)              |
        v                                                              |
+----------------------------------------------------------------------+
|  CAPA 3 — CONFIGURACIÓN / PERSISTENCIA                              |
|    config/ (settings, storage, validation)  ── constantes módulo    |
|    stores SQLite (cards.db compartido por 3 stores — ver §3.4)      |
+----------------------------------------------------------------------+
```

Notas del diagrama:
- La dependencia **descendente** (ui→core→config) es la sana.
- La única dependencia **ascendente** verificada es `ui/state.py:37 → core.qwen_markers`, **justificada**
  como contrato compartido (ver §3.5), no como acoplamiento bidireccional indebido.
- El **punto de convergencia** de todos los hilos de fondo es el *event loop* único de Tk vía
  `_safe_after`. Esa es la frontera de concurrencia que NO debe esconderse tras abstracciones (ver §4).

| Subsistema | Rol / Capa | Frontera de entrada | Frontera de salida | Mecanismo de comunicación |
|---|---|---|---|---|
| **ui** (`app_shell.py` + paneles) | Presentación + Wiring | Construye todo en `__init__`/`_build_ui` | Llama métodos de `core` directamente | `_safe_after` marshaling, `UIState` Observer, `CallbackDispatcher` |
| **core** (`llm_engine`, `health_monitor`, `audio_bed`, stores) | Lógica de negocio | `ui_callback` inyectado, `command_queue` | NO importa `ui` (✅ bien aislado) | Callbacks + colas + Observer; hilos daemon |
| **smart_aggregator** (`aggregator`, `kira_agenda_controller`) | Lógica de negocio (chat + turn-taking) | `process_message`, callbacks de `ChatSource` | Callbacks hacia `ui`; provee prompts al motor | Adapter (`ChatSource` ABC), callbacks |
| **config** (`settings`, `storage`, `validation`, etc.) | Configuración / contrato | Importado por casi todos | Constantes a nivel de módulo | **Imports directos de constantes** (alto fan-out, ver §3.2) |
| **stream_admin** (`admin_manager`, providers) | Integración externa | `AdminManager` instanciado por `app_shell` | OAuth/YouTube API, callbacks UI | Strategy (`BaseStreamProvider`), callbacks |
| **avatar** (`obs_client`, `avatar_state`, `avatar_config`) | Integración externa | `AvatarStateBridge` | OBS WebSocket | **Observer thread-safe** (`avatar_state.py`) — bien ejecutado |
| **entrypoints** (`__main__`, `editorial_cli`, `server_qwen`, `packaging/launcher`) | Bootstrap / CLI / subproceso | argv, atexit | Arranque de `VocalAIApp` o subprocesos | Dispatch fino; `editorial_cli` aislado de UI (✅) |

**Patrón de comunicación dominante:** hilos de fondo (motor, health, OBS retry, chat poller) emiten
eventos que se **marshalan al hilo principal de Tk** vía `_safe_after` (`app_shell.py:2456`). Es una
arquitectura orientada a eventos con un único *event loop* de UI como punto de convergencia.

### 1.3 Corrección importante a un hallazgo de los auditores (inferencia vs. hecho)

Varios auditores afirmaron como **CRÍTICO** que `MotorVocalIA` (hilo daemon) "invoca callbacks de
CustomTkinter directamente en contexto daemon, causando deadlock/corrupción". **Esto es una
exageración.** La evidencia verificada lo contradice:

```python
# app_shell.py:2474-2478 (verificado)
def _on_motor_event(self, status: str) -> None:
    if threading.current_thread() is not threading.main_thread():
        self._safe_after(lambda status=status: self._handle_motor_event(status))
        return
    self._handle_motor_event(status)
```

El callback que el motor invoca (`_on_motor_event`) **detecta el hilo y marshala al hilo principal**.
Es decir, la inversión de hilos **está correctamente manejada en el punto de wiring**. El motor emite
*strings de estado* (`"processing"`, `"ready"`, etc.), no muta widgets directamente. El riesgo real no
es "deadlock garantizado" sino el **descrito en §1.4**: el silenciamiento de excepciones en
`_safe_after` durante el arranque. Marco la afirmación original de los auditores como **incorrecta en su
severidad**; la disciplina de marshaling existe y funciona.

### 1.4 Riesgo real de la frontera de hilos (confirmado)

`_safe_after` traga `RuntimeError` silenciosamente (`app_shell.py:2469-2472`) cuando el motor dispara
eventos antes de que `mainloop()` arranque. Esto es un **defecto de observabilidad real**: eventos
críticos de arranque (modelo cargado, Ollama no disponible) pueden perderse sin log. No es deadlock; es
**pérdida silenciosa de estado**. Severidad: media-alta. Confirmado en código.

---

## 2. Patrones de diseño específicos implementados

Inventario verificado, con evaluación de calidad. Marco "✅ bien aplicado", "⚠️ parcial/frágil" o
"❌ mal aplicado / anti-patrón".

| Patrón | Ubicación (`archivo:línea`) | Evaluación | Evidencia / nota |
|---|---|---|---|
| **Observer (thread-safe)** | `ui/state.py:45` (clase `UIState`), hilo dispatch `state.py:109-117`, `subscribe` `state.py:137` | ✅ **El mejor patrón del repo** | Hilo daemon de dispatch toma snapshot bajo lock y libera ANTES de invocar callbacks (`state.py:184-196`) — evita deadlock reentrante. Setters validan contra frozensets. |
| **Observer (avatar)** | `avatar/avatar_state.py` (`AvatarStateBridge.subscribe/unsubscribe`) | ✅ bien aplicado | OrderedDict + lock; callbacks fuera del lock. Mismo patrón correcto que UIState. |
| **Safe Marshaling al hilo principal** | `app_shell.py:2456` (`_safe_after`), `:2464` chequeo de hilo | ✅ correcto (⚠️ traga `RuntimeError` sin log) | Enruta a `_ui_task_queue` si está fuera del hilo principal; usa `self.after()` si está en él. |
| **Command Queue** | `llm_engine.py:88` (`self.command_queue = queue.Queue()`) | ⚠️ frágil | **Queue sin límite (no `maxsize`)** — verificado. Comandos `('switch_model', tag)`, etc. Sin validación ni back-pressure; riesgo OOM bajo flood. |
| **Strategy (tiers LLM)** | `core/llm_tiers.py` (`LLMTierConfig`/`LLMTierState`), `llm_engine.py:709-784` (`switch_llm_tier`) | ✅ bien aplicado | Dataclass inmutable; rollback en fallo (`:757-777`). Selección sin efectos secundarios. |
| **Strategy (providers de stream)** | `stream_admin/providers.py` (`BaseStreamProvider` ABC), `youtube_provider.py`, `twitch_provider.py` | ✅ contrato; ⚠️ sin factory | YouTube completo, Twitch placeholder. `AdminManager` instancia providers en dict hardcodeado (viola OCP para nuevos providers). |
| **Adapter (fuentes de chat)** | `smart_aggregator/chat_source.py` (`ChatSource` ABC + YouTube/Twitch) | ✅ bien aplicado | Normaliza a `NormalizedChatMessage`; factory en `Aggregator._source_factory`. Limpio para nuevas plataformas. |
| **State Machine (agenda)** | `smart_aggregator/kira_agenda_controller.py:18-32` (`AgendaState` enum, 12+ estados) | ⚠️ disperso | Enum explícito de estados, pero transiciones dispersas en 3 métodos públicos (`mark_generation_accepted`, `mark_speech_complete`, `register_failure`) **y** mutaciones desde `app_shell` (`:1367 kira_agenda.state = AgendaState.OFF`). Sin guard de transición central. |
| **State Machine (Qwen lifecycle)** | `health_monitor.py:230-468` (`QwenProcessManager`) | ⚠️ implícito | 6 estados de ciclo de vida (STARTING→WAITING→READY/FAILED/STOPPED) sin diagrama; transiciones en exception handlers a veces fuera de lock. |
| **Facade (sobre-cargado)** | `smart_aggregator/aggregator.py` (`Aggregator`, 458 líneas verificadas) | ❌ facade con fugas | Expone componentes internos como atributos públicos (`self.msg_filter`, `self.thermometer`, `self.intent_aggregator`); el caller puede saltarse el wrapper. Rompe encapsulación. |
| **Defensive Copying** | `health_monitor.py:591-605` (`state` property reconstruye `MonitorState`) | ✅ correcto; ⚠️ O(N) por lectura | Snapshot fresco bajo lock. Costo de copia en cada lectura del daemon. |
| **Fail-Safe Defaults** | `health_monitor.py:61-114` (`VRAMGuard` con degradación de `pynvml`) | ✅ bien aplicado | Alinea con objetivo local-first (no requiere GPU). ⚠️ reporta 0.0 MB como "critical" cuando debería ser "unavailable". |
| **Conditional Imports (deps opcionales)** | `tts_piper.py:13-18` (`piper.voice`), `obs_client.py` (`obsws_python` import tardío) | ✅ bien aplicado | Degradación elegante; clave para PyInstaller (no obliga a empaquetar TTS pesado). |
| **Callback Dispatcher** | `ui/protocols.py` (`CallbackDispatcher`), wiring en `app_shell.py:160-167` | ⚠️ event names sin tipar | Strings de evento sin validación de tipo; typos son bugs silenciosos. |
| **Singleton implícito** | `MotorVocalIA`/`AudioBedEngine`/`AdminManager` creados una vez en `app_shell` | ⚠️ informal | Sin lock-once; depende de disciplina de `app_shell`. Fuga de estado en tests. |
| **Anti-patrón: bug fixes documentados en comentarios** | `audio_bed.py:68-73, 161-165` ("Bug 2 fix", "Bug 3 fix") | ❌ anti-patrón | El diseño original no previno los estados; los bugs se parchearon post-facto con comentarios+condicionales en vez de un StateEnum. |
| **Anti-patrón: dead code tras feature flag** | `stream_admin_ui.py:365, 438, 493` (`if not STREAM_ADMIN_ENABLED: return`) | ❌ defecto | ~200 LOC RF4 inalcanzables permanecen en el binario. Bloat + carga de revisión. |
| **Shadow Mode (over-engineering)** | `agenda_signal.py:26` (`AGENDA_SIGNAL_SHADOW_MODE`), `chat_input_contract.py` (`INPUT_CONTRACT_SHADOW_MODE`) | ⚠️ deuda especulativa | `AgendaSignal` construido pero **nunca consumido** por el controlador. Confusión para lectores; doble path no testeado. |

**Patrones notablemente AUSENTES** (y correctamente ausentes, dado el pragmatismo):
no hay Dependency Injection container, no hay Repository abstraction sobre SQLite, no hay capa de
puertos/adaptadores sobre Ollama. Su ausencia es **apropiada** para una app local de una sola ventana
(ver §4).

---

## 3. Mantenibilidad: métricas cuantitativas y cualitativas

### 3.1 Tamaños verificados (cuantitativo)

Conteo real (`wc -l`, ejecutado en esta auditoría):

| Archivo | Líneas (verificadas) | Concerns fusionados | Diagnóstico |
|---|---|---|---|
| `ui/app_shell.py` | **3256** | 11 (construcción UI, dispatch, coordinación de hilos, agenda, stream admin, avatar, OBS, PTT, audio, lifecycle, recovery) | God Object crítico |
| `core/llm_engine.py` | **1848** | **8** (ver desglose abajo) | God Object alto |
| `ui/stream_admin_ui.py` | **1659** | 5 (OAuth, metadata, chat, moderación, analytics) | God Object alto + dead code |
| `smart_aggregator/kira_agenda_controller.py` | **1207** | 3+ (gestión de topics, 8 guardrails de validación, política de recovery) | Complejidad alta |
| `ui/cohost_agenda_panel.py` | **907** | 6 (form state, parsing, rendering, validación, batch scheduling, perfiles) | God Object medio |
| `core/health_monitor.py` | **687** | 4 sub-componentes cohesivos (VRAM, Ollama, RTF, Qwen) | Aceptable (bien factorizado internamente) |
| `ui/state.py` | **519** | 1 (contenedor de estado + dispatch) | ✅ cohesión alta; boilerplate repetitivo |
| `config/settings.py` | **493** | 4 (paths, tuning LLM, config TTS, thresholds health) | Catch-all (ver §3.2) |
| `smart_aggregator/aggregator.py` | **458** | 6 (filtro, intent, vibe, activity, history, diagnostics) | God Object medio |

**Desglose de los 8 concerns de `MotorVocalIA`** (clase en `llm_engine.py:79`, **57 métodos
verificados** vía `rg "^    def "`). La nota previa de "~6 concerns" subestimaba la realidad; el conteo
honesto a partir del inventario de métodos es **8 concerns distintos**, con métodos representativos:

| # | Concern | Métodos representativos (`llm_engine.py`) |
|---|---|---|
| 1 | **Dispatch de comandos** | `_dispatch_command` (`:249`), `run` (`:208`) |
| 2 | **Gestión de cola de prioridad / prefetch** | `enqueue` (`:352`), `replace_pending` (`:371`), `_process_priority_queue` (`:581`), `prefetch_agenda` (`:383`), `wait_prefetched_agenda` (`:421`), `play_prefetched_agenda` (`:438`), `enqueue_accumulation` (`:494`), `_flush_accumulation` (`:531`) |
| 3 | **Orquestación del pipeline TTS** | `_hablar` (`:1484`), `_sanitize_tts_text_for_playback` (`:1442`), `is_speaking` (`:189`), `current_speech_source` (`:199`) |
| 4 | **Inferencia LLM + recuperación de timeout** | `_generar_dialogo` (`:951`), `_ejecutar_inferencia` (`:1435`), `_ollama_chat_with_watchdog` (`:1174`), `_resolve_chat_watchdog_timeout` (`:1198`), `_recover_from_stalled_inference` (`:1212`), `_rollback_to_last_known_good_model` (`:1243`) |
| 5 | **Estado de conexión Ollama** | `_check_ollama_service` (`:642`), `_create_ollama_chat_client` (`:1160`), `_ollama_chat` (`:1170`), `_is_ollama_transport_error` (`:1276`) |
| 6 | **Cambio de modelo + gestión de tiers** | `switch_llm_tier` (`:709`), `configure_llm_tiers` (`:693`), `_check_pending_model_switch` (`:786`), `_apply_model_switch` (`:808`), `_switch_and_prepare_model` (`:666`), `_prepare_model` (`:832`), `_download_model_worker` (`:895`), `release_owned_ollama_model` (`:866`) |
| 7 | **Validación de salida de agenda** | `_accept_agenda_output` (`:1282`), `_preview_accept_agenda_output` (`:1292`), `_sanitize_agenda_output` (`:1461`), `_format_agenda_rejection` (`:1304`), `_record_accepted_agenda_output` (`:1319`) |
| 8 | **Digest de memoria + historial** | `_commit_history` (`:1328`), `_sanitize_history_context` (`:1392`), `_build_ledger_line` (`:1379`), `_first_words` (`:1363`), `_first_sentence` (`:1369`) |

Cualquier futura **reducción** de este conteo (p. ej. argumentar que "validación de agenda" y "digest de
memoria" son subdominios del mismo concern de "post-procesado de salida") debe justificarse contra este
inventario, no por estimación. La caracterización honesta es **8 concerns distintos en una sola clase
daemon de 57 métodos** — viola SRP de forma marcada.

**Densidad de `app_shell.py`:** 3256 líneas / 171 métodos ≈ 19 líneas/método promedio, pero la
distribución es **bimodal** (muchos one-liners contra métodos de 50–570 líneas). `_build_ui()` solo es
~570 líneas (`app_shell.py:413-985`). Esto es el indicador cuantitativo más fuerte de complejidad.

### 3.2 Acoplamiento (cualitativo)

**MUY ALTO en dos focos:**

1. **`VocalAIApp` ↔ todo.** `app_shell.py` se acopla a 10+ módulos de paneles, al motor (vía
   `command_queue.put()` + callbacks), al `KiraAgendaController`, `AudioBedEngine`, `OBSClient`,
   `UIState`, `HealthMonitor`, `AgendaPersistence` y `EditorialCardStore`. Todo subsistema habla con el
   shell y el shell habla con todo subsistema. Cambiar la firma de cualquier `core` obliga a tocar
   `app_shell`.

2. **`config/settings.py` por imports de constantes.** 20+ constantes (`DEFAULT_MODEL`, `TEMP_DIR`,
   `EDITORIAL_CARDS_DB`, `PTT_HOTKEY_LIST`, flags `EXPERIMENTAL_HEAVY_TTS_ENABLED`,
   `STREAM_ADMIN_ENABLED`) se importan **a nivel de módulo** en 22+ archivos. No hay capa de abstracción
   de configuración: los cambios en runtime **no se reflejan en la UI sin reiniciar**
   (`app_shell.py:42-51`). Para una app empaquetada de una sola ventana, **reiniciar es aceptable** (ver
   §4), pero el fan-out hace que `settings.py` sea un SPOF de inicialización.

**ACOPLAMIENTO BIEN CONTENIDO (lo positivo):**
`core/` no importa `ui/`. La comunicación es vía `ui_callback` inyectado. `editorial_cli.py` no importa
CustomTkinter (disciplina CLI correcta). `editorial_matching.py` es lógica pura sin dependencias.

### 3.3 Cohesión (cualitativo)

- **MUY BAJA** en `VocalAIApp` (11 concerns) y `MotorVocalIA` (8 concerns): cada uno tiene múltiples
  razones para cambiar (viola SRP de forma marcada).
- **BAJA** en `Aggregator` (6 dominios) y `cohost_agenda_panel` (6 responsabilidades).
- **ALTA** en `UIState`, `health_monitor` (sub-componentes), `editorial_cards`, `editorial_matching`.

### 3.4 Duplicación verificada (cuantitativo)

| Duplicación | Ubicación verificada | Impacto |
|---|---|---|
| **`OBSConfig` definido dos veces, campo por campo idéntico** | `avatar/obs_client.py:31` y `avatar/avatar_config.py:34` (verificado: mismos 6 campos, mismos defaults) | Cambiar un campo (p.ej. `verify_ssl`) requiere editar ambos; bug sutil garantizado |
| **`APP_ID` duplicado** | `server_qwen.py` y `health_monitor.py:239` (`'opencohost-qwen-tts'`) | Sin test de sincronización; divergencia rompe health check silenciosamente |
| **`get_app_dir` duplicado** | `settings.py` y `storage.py` (misma travesía `__file__`) | DRY; lógica de paths divergente |
| **Lógica de selección de motor TTS** | `llm_engine.py:1549-1585` y `health_monitor.py:619-652` | Árbol de decisión fragmentado en 2 módulos |
| **Patrones regex de detección** | `message_filter.py` y `chat_input_contract.py` (`ChatEventDetector`) | 60+ reglas sin librería compartida; riesgo de drift |
| **Tres stores comparten un solo `cards.db`** | `app_shell.py:149, 214, 256` → todos usan `EDITORIAL_CARDS_DB` (verificado) | SPOF de datos: corrupción del archivo tumba editorial + agenda + topic inbox a la vez |

### 3.5 ¿Es mantenible a largo plazo?

**Respuesta: CONDICIONALMENTE SÍ, pero con riesgo creciente.**

**A favor (por qué SÍ, hoy):**
- El core está **bien aislado** de la UI; se puede testear lógica de negocio sin Tk.
- El Observer thread-safe (`UIState`) da una **frontera de concurrencia clara y correcta**.
- Degradación elegante de dependencias opcionales (Piper, OBS, pynvml) — clave para empaquetado.

**Sobre la única dependencia ascendente `ui → core` (justificada, no defecto).**
`state.py:37` importa `ENGINE_STATUSES` desde `core.qwen_markers`:

```python
# ui/state.py:35-37 (verificado)
# Engine (voice) badge statuses — single source of truth lives in core.qwen_markers (SV-A),
# so ui and core can never diverge on the valid set.
from opencohost.core.qwen_markers import ENGINE_STATUSES as VALID_ENGINE_STATUSES
```

Este import **NO es un acoplamiento bidireccional indebido**. Es un **contrato compartido de
fuente-de-verdad-única**: el conjunto válido de estados del badge de motor vive en `core` (donde se
generan los estados) y la UI lo **consume** para validar, en lugar de duplicar el frozenset. El propio
comentario lo declara como invariante (SV-A): "so ui and core can never diverge on the valid set". La
alternativa (duplicar la lista en `ui/`) sería **peor** — reintroduce la clase de bug que el patrón de
fuente única previene (ver `OBSConfig` duplicado en §3.4 como contraejemplo de qué pasa sin este patrón).
**Veredicto: aceptable; no refactorizar.** Es la dirección de dependencia correcta (la UI depende de un
contrato del dominio, no al revés).

**Estrategia y cobertura de tests (con números reales).**
La suite enfocada citada en `CLAUDE.md` —`test_llm_tiers.py`, `test_model_panel.py`,
`test_heavy_model_inference_recovery.py`— arroja **97 tests pasando** (verificado en esta auditoría con
el intérprete del proyecto). La **suite completa colecta 2396 tests** (`pytest --collect-only`). La cifra
"154–159" de notas previas **no coincide con ninguno de los dos conteos** y se considera **dato
obsoleto**; probablemente refería a un subconjunto intermedio de suites en un momento anterior. Para esta
auditoría se usan los números verificados, no la nota heredada.

Observación sobre **distribución de cobertura vs. complejidad** (cualitativa, basada en los nombres y el
alcance de las suites observadas, marcada como **inferencia**):
- Los **God Objects** (`app_shell.py` 3256, `llm_engine.py` 1848) son los archivos de mayor riesgo y
  mayor superficie de hilos, pero las suites de mayor densidad observadas apuntan a **subsistemas
  acotados** (`llm_tiers`, `model_panel`, `heavy_model_inference_recovery`, `health_monitor`), no a la
  orquestación completa de `VocalAIApp`. **Inferencia:** la lógica de tiers/recovery y el panel de modelo
  están bien cubiertos, mientras que el *wiring* y la coordinación multi-hilo del shell —donde más duele
  un bug— quedan **relativamente sub-testeados respecto a su complejidad**, precisamente porque testear
  `VocalAIApp` end-to-end requiere instanciar Tk. Esto **refuerza** el argumento de descomposición: los
  coordinadores extraídos (abajo) serían testeables sin levantar la ventana, subiendo la cobertura
  efectiva del código de mayor riesgo. *(No se midió cobertura por línea en esta auditoría; afirmar un
  porcentaje exacto sería inventar un dato. Queda como inferencia razonada, no como hecho.)*

**En contra (por qué el riesgo crece):**
- `app_shell.py` (3256) y `llm_engine.py` (1848) están en el umbral donde **depurar un bug multi-hilo
  exige leer el archivo entero**. Cada feature nueva empuja `app_shell` hacia 4000+ líneas.
- Las máquinas de estado dispersas (agenda, Qwen lifecycle) hacen que rastrear un freeze cruce 3 archivos
  (motor callbacks → UI callbacks → controller).
- El acoplamiento por constantes de `settings.py` significa que **todo cambio de config es un cambio de
  código**.
- Anti-patrones de "bug fix en comentario" (`audio_bed.py`) y dead code tras flags (`stream_admin_ui.py`)
  predicen que un mantenedor nuevo **reintroducirá bugs ya resueltos**.

**Recomendación de descomposición (la única intervención de alto ROI):** extraer de `VocalAIApp`
coordinadores enfocados (`MotorCoordinator`, `AgendaCoordinator`, `StreamAdminCoordinator`,
`UIBuilder`). Es refactor de *separación*, no de *abstracción*.

**Costeo de la recomendación (para que no quede "sin costear"):**

| Dimensión | Estimación | Notas |
|---|---|---|
| **Esfuerzo** | ~3–5 días-persona | No es mecánico: requiere mover estado y re-cablear callbacks con cuidado. La mayor parte del tiempo es la auditoría de hilos, no el corte de código. |
| **Clases nuevas** | 4 (`MotorCoordinator`, `AgendaCoordinator`, `StreamAdminCoordinator`, `UIBuilder`) | Composición, no jerarquía. `VocalAIApp` pasa a *poseer* coordinadores en vez de estado crudo. |
| **Riesgo principal** | **MEDIO-ALTO: thread-safety** | El mayor riesgo es romper la frontera de marshaling (`_safe_after` / `_on_motor_event`). **Obligatorio**: auditoría de hilos durante la descomposición y re-ejecución de la suite de recovery/health antes de mergear. No es un refactor "a ciegas". |
| **Impacto en bundle PyInstaller** | **Negligible** | Cero dependencias nuevas; los mismos imports redistribuidos en más módulos internos del paquete. PyInstaller empaqueta por dependencia, no por número de archivos `.py` internos. |
| **Beneficio esperado** | `app_shell.py` de **3256 → ~2000 LOC**; `MotorVocalIA` de **1848 → ~1200 LOC** | Responsabilidad única más clara por coordinador; **testing de coordinadores sin instanciar Tk** (cierra el hueco de cobertura de §3.5); depuración de freezes acotada a un coordinador en vez de cruzar 3 archivos. |

La descomposición es la intervención correcta, pero **no es gratis ni libre de riesgo**: su valor depende
de tratar la auditoría de thread-safety como parte no-opcional del trabajo, no como un extra.

---

## 4. Regla de pragmatismo — "mejoras" que serían DEFECTOS

El prompt exige nombrar explícitamente cuándo una idea de "arquitectura limpia" dañaría peso de bundle,
empaquetado, o thread-safety/perf de UI. Para cada rechazo doy el **tradeoff razonado** (costo de
adoptarla vs. beneficio que prometería), no solo el veredicto:

| Propuesta del auditor | Costo de adoptarla | Beneficio que prometería | Veredicto razonado |
|---|---|---|---|
| **Bus de eventos genérico unificado** (reemplazar `_safe_after` + `CallbackDispatcher` + dict dispatch) | **Riesgo de correctness alto:** un bus genérico **oculta** la frontera de hilo. La thread-safety actual es correcta porque el marshaling es **explícito y verificable** (`_on_motor_event` chequea `current_thread()`). Una capa de bus difumina dónde ocurre el cruce de hilo → más fácil de romper en un cambio futuro. | Callbacks "más desacoplados"/centralizados (beneficio de legibilidad menor). | ❌ **Rechazar.** El costo (riesgo de correctness en la frontera de hilos, que es el activo más valioso del repo) supera por mucho el beneficio (legibilidad marginal). Cero ganancia runtime. |
| **Capa de abstracción/puertos sobre Ollama** (`LLMProvider` interface) | Superficie de código nueva sin un segundo proveedor real; más carga cognitiva; YAGNI. | Permitiría swappear de backend LLM si apareciera uno. | ❌ **Rechazar hasta que exista 2º backend.** Costo presente (código muerto + ceremonia) vs. beneficio hipotético (un backend que hoy no existe). Reconsiderar **solo** si el roadmap añade un segundo proveedor. |
| **Adapter/Facade sobre `pygame.mixer`** para swappear backend de audio | **Bloat de bundle:** PyInstaller ya empaqueta pygame; el adaptador agrega código para un backend hipotético (PyAudio/PulseAudio) que nunca se empaqueta → bytes muertos en el `.exe`. | Flexibilidad de backend de audio teórica. | ❌ **Rechazar.** Costo concreto (bundle más pesado) vs. beneficio nulo en la práctica (un solo backend real). |
| **Cargar `MODELS_CATALOG` desde YAML en runtime** en vez de dict en `settings.py` | I/O de arranque extra + riesgo de path no empaquetado por PyInstaller. | **Extensibilidad de usuario:** un usuario podría añadir un modelo Ollama local **sin editar `settings.py` ni recompilar**. | ⚠️ **Bajo ROI para el producto actual** (local-first, sin API de extensión de usuario), **PERO sería ALTO ROI si/ cuando se requieran catálogos de modelos provistos por el usuario.** Hoy un usuario **no puede** añadir un modelo local sin editar `settings.py` y rebuildear — limitación real, aunque tolerable dado el alcance actual. **Marcar como diferido pendiente de decisión de roadmap, no rechazado permanentemente.** |
| **`ProfileStore` con DI de I/O para testabilidad** | Ceremonia de inyección sobre código que ya funciona. | Testear I/O de perfiles aisladamente. | ⚠️ **Diferir.** Solo vale si surge necesidad concreta de testear el I/O de perfiles de forma aislada; hoy el costo (boilerplate) supera el beneficio. |
| **Lazy-load de paneles para reducir startup** | **Riesgo real de romper el wiring** (los paneles se cablean entre sí en `_build_ui`); complejidad de orquestación de carga diferida. | Startup más rápido / menor footprint de arranque. | ⚠️ **No rechazar ni adoptar sin medir primero.** El argumento de "Tkinter ya hace layout lazy" es **insuficiente** sin datos. **Falta baseline:** no se midió tiempo de arranque, tiempo de carga por panel, ni footprint de memoria. Para una app de cara al usuario la latencia de arranque importa. **Acción correcta: medir antes de decidir** (instrumentar arranque y carga de paneles); recién con números decidir si el ahorro justifica el riesgo de wiring. *(Sin medición, cualquier afirmación de "marginal" o "vale la pena" es especulación.)* |
| **Refactor de `_build_ui` en sub-factories** (570 líneas, `app_shell.py:413`) | Bajo: corte mecánico en sub-factories por grupo de paneles (header / main_frame / footer). | **Legibilidad/mantenibilidad estática:** 570 líneas de definición declarativa de UII en un método son objetivamente más difíciles de revisar y mantener que módulos acotados. | ✅ **Hacer, por legibilidad estática — no por hot-reload.** Aclaración: **sí es una carga de mantenibilidad** (un método de 570 líneas es difícil de revisar). El objetivo NO es flexibilidad runtime (Tkinter no soporta hot-reload, ese argumento es irrelevante aquí) sino **legibilidad y revisabilidad estática**. Partir `_build_ui` en sub-factories por grupo de paneles reduce la carga cognitiva de revisión sin tocar el comportamiento. Encaja naturalmente dentro de la descomposición a `UIBuilder` de §3.5. |

**Propuestas de los auditores que SÍ tienen ROI real (no son over-engineering):**

1. **Descomponer `VocalAIApp` en coordinadores** (refactor de separación, no de abstracción) — §3.5, ya costeada.
2. **Acotar `command_queue` con `maxsize` + manejo de `Full`** (`llm_engine.py:88`) — previene OOM real.
3. **Unificar `OBSConfig`** en una sola definición — elimina bug latente.
4. **Loggear en `_safe_after` cuando se traga `RuntimeError`** — cierra el agujero de observabilidad.
5. **Test que sincronice `APP_ID`** entre `server_qwen.py` y `health_monitor.py` — previene fallo silencioso.

---

## 5. Hallazgos clave para verificación (resumen)

| # | Afirmación | Evidencia | Confianza |
|---|---|---|---|
| 1 | `app_shell.py` = 3256 líneas, God Object con 11 concerns | `wc -l` verificado | **Alta (hecho)** |
| 2 | `llm_engine.py` = 1848 líneas; `MotorVocalIA` daemon (`:79`), 57 métodos, **8 concerns** | `wc -l` + `rg "^    def "` + inventario de §3.1 verificados | **Alta (hecho)** |
| 3 | La inversión de hilos motor→UI **está marshalada** (no deadlock garantizado) | `app_shell.py:2474-2478` verificado | **Alta — corrige a los auditores** |
| 4 | `_safe_after` traga `RuntimeError` sin log (agujero de observabilidad) | `app_shell.py:2469-2472` verificado | **Alta (hecho)** |
| 5 | `OBSConfig` duplicado idéntico | `obs_client.py:31` + `avatar_config.py:34` verificado | **Alta (hecho)** |
| 6 | Tres stores comparten `cards.db` (SPOF) | `app_shell.py:149,214,256` verificado | **Alta (hecho)** |
| 7 | `command_queue` sin límite | `llm_engine.py:88` verificado | **Alta (hecho)** |
| 8 | `UIState` Observer thread-safe correctamente implementado | `state.py:109-196` verificado | **Alta (hecho)** |
| 9 | Docstring de `app_shell.py:1-9` ("thin composition, no inline UI") **contradice** la realidad (estado de negocio + `_build_ui` 570 LOC inline) | `app_shell.py:1-9` vs. `:148-214` y `:413` verificados | **Alta (hecho) — corrige el docstring** |
| 10 | `ui/state.py:37 → core.qwen_markers` es contrato de fuente única (SV-A), **justificado** | comentario + import en `state.py:35-37` verificados | **Alta (hecho)** |
| 11 | Suite enfocada de `CLAUDE.md` = **97 tests pasando**; suite completa = **2396 colectados**; "154–159" es dato obsoleto | `pytest` + `pytest --collect-only` ejecutados en esta auditoría | **Alta (hecho)** |
| 12 | La arquitectura es "capas pragmáticas + eventos", no hexagonal/MVC | Inferencia estructural sobre imports y wiring | **Media (inferencia razonada)** |
| 13 | Mantenibilidad = CONDICIONAL (riesgo crece con tamaño de `app_shell`) | Juicio cualitativo sobre métricas verificadas | **Media (juicio)** |
| 14 | El *wiring*/coordinación multi-hilo del shell está sub-testeado respecto a su complejidad | Inferencia sobre alcance de suites observadas (no se midió cobertura por línea) | **Baja-Media (inferencia, NO hecho)** |

---

*Fin del diagnóstico. Este documento es solo análisis; no se modificó código fuente.*
