# 04 — Efficiency

- **Fecha**: 2026-06-29 · **Branch**: `maintenance/big-file-audit-small-fixes-20260629` · **Commit base**: `6d190b1`
- **Método**: hallazgos de eficiencia de los subagentes, verificados por el juez. Solo se aplica si es chico + seguro + demostrable.

| ID | File:line | Trabajo innecesario | Evidencia | Acción |
|---|---|---|---|---|
| **BFA-6** | stream_admin_ui.py:1170 | construía `list(self._chat_users.keys())` (~1001 entradas) para borrar ~1, en el hot path por-mensaje bajo `_chat_lock` | eviction probada equivalente con `next(iter())` (drops oldest-inserted, len→1000) | **APLICADO** (`b57d6d3`) |
| BFA-llm-1 | llm_engine.py:678–692 | `_pq_lock` retenido a través de inferencia + duración de TTS en la rama de acumulación (la rama hermana suelta el lock antes) | sección crítica innecesariamente ancha | SDD (LLM hardening — angostar el lock) |
| BFA-app_shell-2 | app_shell.py:383–400 | `load_avatar_config` (YAML sin cache) + `Image.open`/`thumbnail`/`CTkImage` en el main thread por cada cambio de estado no-idle (solo idle cachea) | IO + decode síncrono en el hilo Tk | SDD (logging/perf hot path — cachear como idle) |
| BFA-app_shell-5 | app_shell.py:1645 | `_agenda_persistence.save_if_changed` síncrono en el hilo Tk dentro del funnel | mitigado: no-op si no cambió → costo es change-detection, no escritura | SDD / document_only |
| BFA-app_shell-4 | app_shell.py:2266–2311 | rebuild de dict ~45 claves por evento de motor + `_tick_speaking_alt` caliente | churn dominado por construcción de dict (6 lambdas, no 15 — corrección del juez) | document_only (micro) |
| BFA-launcher-7 | launcher.py:902–03 | re-import + `EnumWindows` por poll (~240/launch) | nit: re-import es lookup cacheado en sys.modules | document_only |
| BFA-KAC-3 / KAC-7 | kira_agenda_controller.py:959 / 559,620 | `rejection_log` sin trim + `get_metrics` O(n); `topics.index` en sort key O(n² log n) | real pero negligible a los conteos actuales | document_only |

**Aplicado**: 1 (BFA-6). **A SDD**: BFA-llm-1, BFA-app_shell-2. **document_only**: el resto (negligible o requiere cambio arquitectónico).
