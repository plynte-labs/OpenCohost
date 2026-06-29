# 02 — Usage / Wiring Map

- **Fecha**: 2026-06-29 · **Branch**: `maintenance/big-file-audit-small-fixes-20260629` · **Commit base**: `6d190b1`
- **Comandos**: `rg -l "<modulo>" --glob '*.py'`; `rg -c '^class '/'^def '/'^    def '`; subagentes de auditoría trazaron rutas de llamada (incl. cross-file).
- **Inspeccionados**: P1–P5.

## Wiring por archivo
| Archivo | Entrada / quién lo usa | Nota |
|---|---|---|
| **P1** app_shell.py | instanciado por `app.py`; referencian motor_event_handlers, ptt_manager, obs_lifecycle, gear_popover, aggregator, context_budget, llm_engine | Núcleo. Recibe callback del aggregator; `smart_aggregator/chat_source.py:101/219` lo alimenta desde un **daemon thread** → 5 sitios usan `self.after(0, lambda…)` crudo (BFA-app_shell-6 → SDD). |
| **P2** llm_engine.py | settings, context_budget, app_shell, qwen_markers, runtime_smoke_harness | Clase única; worker thread + watchdog daemon. |
| **P3** launcher.py | **solo** `packaging/build_release_meta` | Aislado; entrada por `main()`. |
| **P4** stream_admin_ui.py | settings, app_shell, legacy/stream_admin_shell_legacy | Gated `STREAM_ADMIN_ENABLED`. |
| **P5** kira_agenda_controller.py | agenda_persistence, editorial_agenda_bridge, app_shell, topic_inbox_bridge, smart_aggregator/__init__ | Orquestador de agenda. |

## Cableado muerto / confuso (hallazgos verificados por el juez)
- **BFA-1** (admin): `_build_agenda_tab` (:642) **nunca se llama** → `set_agenda_status` (:894) es no-op silencioso aunque `app_shell.py:1664` lo invoca; setters/dispatchers (:759–892) muertos. → **SDD** (removerlo ramifica en app_shell).
- **BFA-app_shell-9**: `_main_view_buttons={}` (:495) nunca poblado; loop de recolor (:2009–2013) itera dict vacío. → document_only (core).
- **BFA-launcher-8**: param `notify` (:867) muerto, pero la fn la referencia `build_release_meta`. → document_only.
- **BFA-3** (admin): `_chat_stop`/`_chat_thread` (:74–75) muertos (thread es propiedad de app_shell). → document_only (low).

Engram: `loop/big-file-audit-20260629`.
