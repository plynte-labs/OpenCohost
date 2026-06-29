# 05 — Fix Candidates

- **Fecha**: 2026-06-29 · **Branch**: `maintenance/big-file-audit-small-fixes-20260629` · **Commit base**: `6d190b1`
- 9 candidatos `fix_now` confirmados por el juez. **6 aplicados** (no-core), **3 diferidos** (core / riesgoso → requieren OK explícito por reglas 3/4/10).

## Aplicados (6) — no-core, behavior-preserving o bug chico validado
| ID | File:line | Cambio | Commit |
|---|---|---|---|
| BFA-launcher-4 | launcher.py:425 | pad de tuplas de versión (bug) | `02f36c0` |
| BFA-launcher-1 | launcher.py:1286 | `reporter.detail("NOTE: …")` (additive) | `02f36c0` |
| BFA-5 | stream_admin_ui.py:1589–91/1598 | `result` muerto + import local | `b57d6d3` |
| BFA-6 | stream_admin_ui.py:1170 | eviction `next(iter())` | `b57d6d3` |
| BFA-7 | stream_admin_ui.py:1066 | tags one-comprehension | `b57d6d3` |
| BFA-KAC-8 | kira_agenda_controller.py:511–512 | borra rama `dinamico` no-op | `35bb610` |

## Diferidos (3) — pendientes de OK del owner
| ID | File:line | Cambio | Por qué se difiere |
|---|---|---|---|
| BFA-app_shell-1 | app_shell.py:713–714 | borrar duplicado verbatim (net −2) | **Core P1** — regla 4 (extra cuidado). Trivial y seguro según el juez; solo falta tu OK. |
| BFA-llm-8 | llm_engine.py:1625/1647/1489–90 | quitar `import re`/`import ollama` redundantes, usar `self.ollama` | **Core P2** — extra cuidado. Behavior-preserving según el juez. |
| BFA-llm-7 | llm_engine.py:121/332/735/886 **+ tests/test_llm_engine_model_trace.py** (set-sites 180/205/245/285/360/587/599 + asserts 214/220) | borrar estado muerto `_pending_switch_retries` | **Core P2 + 2 archivos**: borra asserts de test. El juez confirmó que el auditor se equivocó ("tests quedan verdes" es falso). Mecánico pero amerita tu firma porque remueve cobertura. |

## Criterio de corte aplicado
Aceptado `fix_now` solo si: <~50 líneas netas, localizado, sin migración/rename masivo, sin cambio de API pública (salvo bug obvio), reversible, con test/validación clara. Todo lo demás → `08_sdd_proposals.md` o document_only (`03`/`04`).
