# 08 — SDD Proposals (routing)

- **Fecha**: 2026-06-29 · **Branch**: `maintenance/big-file-audit-small-fixes-20260629`
- Hallazgos grandes/arquitectónicos → NO se tocan en código. Detalle completo en `docs/sdd_tracks/big_file_decomposition_20260629.md`.

| Categoría | Finding(s) | File:line | Resumen |
|---|---|---|---|
| **UI/App Shell decomposition** | BFA-app_shell-7, BFA-app_shell-10, BFA-4(admin) | app_shell.py / stream_admin_ui.py:702–892 | God constructor + import threading triplicado; método de routing ~85 líneas con fallback SECURITY raw-chat inline (→ folder en track Raw-Chat); pares set/dispatch 1:1 a registry (low ROI). |
| **Thread/timer lifecycle** | BFA-app_shell-3, BFA-app_shell-6, BFA-app_shell-5 | app_shell.py:303/2744, :1664–1693, :1645 | Loops de reschedule sin guard `_closing`; 5 `self.after(0,…)` crudos en path alimentado por daemon thread (**bug latente** confirmado vía chat_source.py); persistencia síncrona en hilo Tk. |
| **LLM startup/runtime hardening** | BFA-llm-1, BFA-llm-4, BFA-llm-5, BFA-KAC-1, BFA-KAC-2(+ADD) | llm_engine.py:678–692/1142–1234/1756, kira_agenda_controller.py:858/900–908 | `_pq_lock` ancho; budgets de retry compartidos → retorno vacío silencioso; `_hablar`/`productor` ~366 líneas; recovery ladder retorna antes de degrade/pause; salvage-trim saltea guardrails. |
| **Logging/perf hot path** | BFA-app_shell-2 | app_shell.py:383–400 | YAML sin cache + decode de imagen en hilo Tk por cambio de estado no-idle; cachear como idle. |
| **Dead wiring / duplicate pathways** | BFA-1(admin) | stream_admin_ui.py:642/759–897 | `_build_agenda_tab` nunca llamado → `set_agenda_status` no-op aunque app_shell lo invoca; remover ramifica en app_shell. |
| **Install/bootstrap integrity (security)** | BFA-launcher-2 + ADD-uv | launcher.py:536/630, :65 | `expected_sha256=None` saltea verify; uv desde `latest` sin pin. Pin + verify digest (pin ≠ verify). |

## Categorías SIN hallazgos en esta auditoría
OBS lifecycle/resource cleanup; RAM/OOM recoverability (gaps relacionados son document_only: BFA-llm-9/10); Config surface simplification.
