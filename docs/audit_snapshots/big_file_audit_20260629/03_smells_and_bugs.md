# 03 — Smells & Bugs

- **Fecha**: 2026-06-29 · **Branch**: `maintenance/big-file-audit-small-fixes-20260629` · **Commit base**: `6d190b1`
- **Método**: 5 subagentes opus de auditoría (lectura completa por archivo) → 5 jueces opus adversariales que verificaron CADA línea citada. **42 hallazgos base + 4 adds, 0 rechazados.**
- **Evidencia íntegra**: workflow `wf_64eb1a96-25b` (transcript persistido). Aquí el registro consolidado.

## Bugs confirmados (chicos, fixeables)
| ID | File:line | Qué | Acción |
|---|---|---|---|
| BFA-launcher-4 | launcher.py:425 | `(1,2) >= (1,2,0)` False → re-bootstrap espurio de la misma versión | **APLICADO** (`02f36c0`) |

## Bugs probables / latentes (no fixeables como chico → SDD/doc)
| ID | File:line | Qué | Acción |
|---|---|---|---|
| BFA-app_shell-6 | app_shell.py:1664/1670/1682/1691/1693 | `self.after(0, lambda…)` crudo en path alimentado por daemon thread (chat_source) → race de thread-safety latente | SDD (thread/timer) |
| BFA-llm-4 | llm_engine.py:1142–1234 | `max_intentos=2` compartido por 3 self-heals → overflow(intento0)+reasoning-cap(intento1) sale con `raw_content=""` (retorno vacío silencioso) | SDD (LLM hardening) |
| BFA-KAC-1 | kira_agenda_controller.py:858 | `register_failure` retorna antes de degrade/pause para topics activos | SDD (recovery ladder) |
| BFA-KAC-2 (+ADD-1) | kira_agenda_controller.py:900–908 | salvage-trim llama `record_success`/`return True` antes de guardrails (leak/inner-life/character); preview(mutate=False) y accept divergen en texto trailing-dup | SDD (recovery ladder) |
| BFA-llm-6 | llm_engine.py:1187 vs 1204/1242 | `.get` vs `getattr` sobre el mismo `respuesta` → silent-0 si algún día fluye un dict plano | document_only (sin bug hoy) |
| BFA-launcher-2 (+ADD uv) | launcher.py:536/630, :65 | `expected_sha256=None` saltea `verify_sha256`; uv desde `latest` sin pin | SDD (security) |
| BFA-launcher-5 | launcher.py:1452–54 | doble `os.replace`, solo OSError capturado → ventana de crash | document_only |

## Smells confirmados (no ameritan cambio ahora salvo los marcados aplicados)
| ID | File:line | Qué | Acción |
|---|---|---|---|
| BFA-KAC-8 | kira_agenda_controller.py:511–512 | rama `dinamico` no-op | **APLICADO** (`35bb610`) |
| BFA-5 | stream_admin_ui.py:1589–91/1598 | `result` muerto + `import threading` redundante | **APLICADO** (`b57d6d3`) |
| BFA-7 | stream_admin_ui.py:1066–67 | `tags_raw` dual-typed + isinstance | **APLICADO** (`b57d6d3`) |
| BFA-app_shell-1 | app_shell.py:713–714 | duplicado verbatim de :710–711 | **DIFERIDO** (core, pendiente OK) |
| BFA-llm-8 | llm_engine.py:1625/1647/1489–90 | `import re`/`import ollama` redundantes | **DIFERIDO** (core, pendiente OK) |
| BFA-llm-7 | llm_engine.py + test_llm_engine_model_trace.py | estado muerto `_pending_switch_retries` (2 archivos, borra asserts de test) | **DIFERIDO** (riesgoso, pendiente OK) |
| BFA-1, BFA-3, BFA-app_shell-9, BFA-launcher-8 | (ver 02) | cableado/dead code | SDD / document_only |
| BFA-KAC-4 | kira_agenda_controller.py:669–680 | `can_auto_resume` muta estado (predicate con side-effects) | document_only (rename = API pública) |
| BFA-KAC-5/6/7/9/10, BFA-llm-9/10, BFA-launcher-3/6/7/9/10, BFA-2/8/9/10/11 (admin) | (ver synthesis) | smells/lifecycle/perf menores | document_only |

## Correcciones del juez (trazabilidad)
- BFA-app_shell-4: "~15 lambdas" → **6** reales (resto son lookups getattr).
- BFA-llm-7: el auditor dijo "tests quedan verdes" → **FALSO**, los tests SET y ASSERT el campo → es cambio de **2 archivos**.
- BFA-KAC-5: cita corregida (665/686 son `=0`, no sync a recovery).
- Severidades bajadas: launcher-2 high→med-high; admin BFA-2/3 med→low.
