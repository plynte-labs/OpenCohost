# 01 — Inventario de archivos >1000 líneas

- **Fecha**: 2026-06-29
- **Branch**: `maintenance/big-file-audit-small-fixes-20260629`
- **Commit base**: `6d190b1`
- **Comandos**:
  ```
  git ls-files '*.py' | (wc -l por archivo) | filtro >1000 | sort -rn
  rg -c '^class ' / '^def ' / '^    def '   # estructura
  rg -l "<modulo>" --glob '*.py'            # importadores (direccional)
  ```
  `git ls-files` excluye automáticamente artifacts/.venv/envs/build/caches/node_modules (no trackeados).

## Resumen
**13 archivos >1000 LOC**: 5 de producción (superficie real), 8 de tests (deprioritizados — solo se auditan si un fix de producción toca su contrato).

## Producción

| ID | Archivo | LOC | Clases | Métodos | Rol aparente | Clasificación | Riesgo | ¿SDD? |
|---|---|---|---|---|---|---|---|---|
| **P1** | `opencohost/ui/app_shell.py` | 2844 | 3 | 141 | UI shell + límite thread-safety | **God object / App shell** | ALTO | Sí — ya en `ui_rendering_optimization_20260609` (guard de line-count) |
| **P2** | `opencohost/core/llm_engine.py` | 2127 | 1 | 62 | Orquestación LLM + tier switching | **Orchestrator monolítico (1 clase)** | ALTO | Probable |
| **P3** | `packaging/launcher.py` | 1675 | 7 | 30 (+53 top-defs) | Launcher de empaquetado | Script procedural | MEDIO (aislado: solo `build_release_meta` lo referencia) | A evaluar |
| **P4** | `opencohost/ui/stream_admin_ui.py` | 1633 | 1 | 109 | UI admin de stream (RF4) | UI controller | MEDIO (gated `STREAM_ADMIN_ENABLED`) | A evaluar |
| **P5** | `opencohost/smart_aggregator/kira_agenda_controller.py` | 1328 | 8 | 86 | Orquestador de agenda de Kira | Controller/orchestrator | MEDIO-ALTO | Probable |

### Importadores / referencias (rg -l, direccional — aristas finas → Fase 3)
- **P1 app_shell**: launcher, app.py, motor_event_handlers, ptt_manager, obs_lifecycle, gear_popover, aggregator, context_budget, llm_engine, editorial_cli, runtime_smoke_harness, legacy/stream_admin_shell_legacy → **núcleo, muy referenciado**.
- **P2 llm_engine**: settings, context_budget, app_shell, qwen_markers, runtime_smoke_harness, scripts/delegate.
- **P3 launcher**: solo `packaging/build_release_meta` → **aislado**.
- **P4 stream_admin_ui**: settings, app_shell, legacy/stream_admin_shell_legacy.
- **P5 kira_agenda_controller**: agenda_persistence, editorial_agenda_bridge, app_shell, topic_inbox_bridge, smart_aggregator/__init__, runtime_smoke_harness.

## Tests >1000 LOC (deprioritizados)
test_kira_agenda_controller (1504), test_smart_aggregator_ui (1440), test_app_shell_obs_resilience (1421), test_stream_admin_ui (1380), test_voice_control (1234), test_advanced_panel (1191), test_smart_aggregator (1182), test_model_panel (1174).

## Decisión
Auditar **P1–P5** en el loop. P1/P2 son núcleo (reglas 3/4): NO se tocan más allá de fix mínimo quirúrgico; cualquier descomposición → SDD `big_file_decomposition_20260629`. P3/P4/P5 son donde más probablemente haya fixes chicos accionables.

## Riesgo
Alto en P1/P2 si se toca de más → mitigado por la regla de "solo fix mínimo + SDD para lo demás".

## Siguiente acción
Fase 3 loop (Usage/Wiring → Smell/Bug → Efficiency → Fix-candidate → Validation) con verificación adversarial por hallazgo. Scope/profundidad **a confirmar con owner** por presupuesto.

## Engram key
`voiceai` — `loop/big-file-audit-20260629` (init).
