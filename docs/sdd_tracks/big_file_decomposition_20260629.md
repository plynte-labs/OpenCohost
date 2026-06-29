# SDD Track — Big-File Decomposition & Hardening (20260629)

**Status**: Proposal (no code) · **Branch origen del hallazgo**: `maintenance/big-file-audit-small-fixes-20260629` · **Commit base**: `6d190b1`
**Fuente**: auditoría `big_file_audit_20260629` (5 archivos >1000 LOC, 5 jueces opus). Engram: `loop/big-file-audit-20260629`.

Estos ítems NO se tocan ahora (exceden el criterio de fix chico: cambian comportamiento observable, tocan arquitectura/contratos, o ramifican entre módulos). Cada uno es un futuro change SDD con alcance mínimo.

---

## Prioridad ALTA

### SDD-BFA-A — `self.after` crudo en path alimentado por thread (latente)
- **Problema**: `app_shell.py:1664/1670/1682/1691/1693` usan `self.after(0, lambda…)` crudo en vez de `_safe_after` (que tiene guard de hilo en :2231). El callback `on_aggregated_context` lo dispara `smart_aggregator/chat_source.py:101/219` desde un **daemon thread** → cruce de hilo a Tk sin la barrera.
- **Archivo**: `opencohost/ui/app_shell.py`
- **Por qué no ahora**: aunque el swap parece mecánico, el juez pidió un trace completo del caller a través de cualquier cola intermedia antes de tocar el boundary de thread-safety (no drive-by).
- **Riesgo actual**: medio — race intermitente bajo carga de chat; no observado como crash en el log de runtime, pero es real.
- **Diseño futuro**: cambiar los 5 sitios a `_safe_after` + assertion de origen de hilo; trace del path completo.
- **Alcance mínimo**: 5 sitios + 1 assertion. **Pruebas**: test de invocación off-main-thread que afirme que el dispatch pasa por la cola. **Aceptación**: no `after` crudo en paths cross-thread; suite verde.
- **Prioridad**: ALTA (corrección de bug latente).

### SDD-BFA-B — Budgets de retry compartidos → retorno vacío silencioso
- **Problema**: `llm_engine.py:1142–1234` comparte `max_intentos=2` entre 3 self-heals; overflow-trim (intento 0) + reasoning-cap drop (intento 1) salen del loop con `raw_content=""` → respuesta vacía silenciosa.
- **Archivo**: `opencohost/core/llm_engine.py` (CORE).
- **Por qué no ahora**: extracción/separación de budgets en un path core de inferencia — cambia control flow.
- **Riesgo actual**: medio — respuesta vacía rara bajo doble self-heal; observable como turno mudo.
- **Diseño futuro**: budgets de retry separados por capa (overflow / reasoning-cap / empty).
- **Alcance mínimo**: refactor del loop de `_generar_dialogo`. **Pruebas**: caso que fuerce overflow+reasoning-cap y afirme salida no vacía. **Aceptación**: ningún path de doble self-heal retorna `""`.
- **Prioridad**: ALTA.

### SDD-BFA-C — Integridad de bootstrap (security)
- **Problema**: `launcher.py:630` saltea `verify_sha256` cuando `expected_sha256=None` (:536); uv se baja desde `latest` sin pin (:65).
- **Archivo**: `packaging/launcher.py`.
- **Por qué no ahora**: cruza el boundary de seguridad (fetch de manifest/digest + pin) — >50 líneas, no quirúrgico.
- **Riesgo actual**: medio-alto — instalación no verificada/no reproducible; HTTPS mitiga MITM común pero no es defensa en profundidad.
- **Diseño futuro**: pin de versión uv conocida-buena **y** verify de su digest publicado (pin ≠ verify).
- **Alcance mínimo**: constante de versión uv + fetch+compare de digest. **Pruebas**: unit de verify con digest correcto/incorrecto. **Aceptación**: install falla cerrado ante digest mismatch; uv pineado.
- **Prioridad**: ALTA (release/security).

---

## Prioridad MEDIA

### SDD-BFA-D — Recovery ladder de agenda salta degrade/pause
- **Problema**: `kira_agenda_controller.py:858` retorna antes de los checks de degrade (864)/pause (866) para topics activos; salvage-trim (:900–908) llama `record_success`/`return True` antes de guardrails de leak/inner-life/character (:915–944); preview (mutate=False) y accept divergen en texto trailing-dup.
- **Archivo**: `opencohost/smart_aggregator/kira_agenda_controller.py`.
- **Riesgo actual**: medio — recuperación incompleta + divergencia preview/accept.
- **Diseño futuro**: unificar el orden guardrail→record en ambos paths; reconciliar preview/accept.
- **Alcance mínimo**: reordenar el bloque salvage + el early-return. **Pruebas**: caso trailing-dup que afirme mismos guardrails en preview y accept. **Aceptación**: paths convergen.
- **Prioridad**: MEDIA.

### SDD-BFA-E — `_pq_lock` ancho + `load_avatar_config` en hilo Tk
- **Problema**: `llm_engine.py:678–692` retiene `_pq_lock` a través de inferencia+TTS en la rama de acumulación (la hermana lo suelta antes); `app_shell.py:383–400` hace YAML sin cache + `Image.open/thumbnail/CTkImage` en el main thread por cambio de estado no-idle.
- **Archivos**: `llm_engine.py` (CORE), `app_shell.py` (CORE).
- **Riesgo actual**: bajo-medio — contención/jank perceptible bajo carga.
- **Diseño futuro**: angostar la sección crítica; cachear avatar como ya se hace con idle (:398).
- **Alcance mínimo**: mover el lock release; cache dict de avatar. **Pruebas**: micro-bench antes/después. **Aceptación**: lock no cubre TTS; avatar cacheado.
- **Prioridad**: MEDIA.

---

## Prioridad BAJA

### SDD-BFA-F — Decomposition de god-objects
- **Problema**: `app_shell.py` (god constructor, 141 métodos, import threading triplicado en :15/209/1082); `_hablar`/`productor` ~366 líneas (llm_engine.py:1756); pares set/dispatch 1:1 en stream_admin_ui.py:702–892.
- **Riesgo actual**: bajo (mantenibilidad, no correctness).
- **Diseño futuro**: extracción incremental; coordinar con `ui_rendering_optimization_20260609` (que ya posee el guard de line-count de app_shell).
- **Alcance mínimo**: una extracción por PR, sin cambio de comportamiento. **Pruebas**: suite existente verde por extracción. **Aceptación**: LOC baja sin regresión.
- **Prioridad**: BAJA (post-release).

### SDD-BFA-G — Dead wiring de agenda en stream_admin
- **Problema**: `stream_admin_ui.py:642` `_build_agenda_tab` nunca llamado → `set_agenda_status` (:894) no-op aunque `app_shell.py:1664` lo invoca; setters/dispatchers :759–892 muertos.
- **Riesgo actual**: bajo (código muerto, pero remover ramifica en app_shell).
- **Diseño futuro**: decidir wire-or-remove; si remove, limpiar el call en app_shell.
- **Alcance mínimo**: remover el tab muerto + el call. **Pruebas**: suite stream_admin + app_shell verde. **Aceptación**: sin no-ops silenciosos.
- **Prioridad**: BAJA.

---

## Notas
- Categorías sin hallazgos: OBS lifecycle, RAM/OOM recoverability, Config surface (los gaps de unload son document_only BFA-llm-9/10).
- Document-only (no SDD, no fix): ver `03_smells_and_bugs.md` / `04_efficiency.md`.
