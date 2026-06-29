# 10 — Core Fixes: SDD Design → Judge → Apply (gate)

- **Fecha**: 2026-06-29 · **Branch**: `maintenance/big-file-audit-small-fixes-20260629`
- **Proceso (a pedido del owner)**: diseño SDD → 2 jueces opus adversariales independientes validan alcance+correctitud contra el código real → recién ahí aplicar.
- **Workflow**: `wf_eb8f0fde-988` (1 diseño + 2 jueces + síntesis).

## Veredicto del gate
Ambos jueces APPROVE en los 3, con **una corrección material** que justifica el gate: el diseño original de FIX-2 incluía un 3er edit (swap `ollama.show`→`self.ollama.show`) que **rompería ≥4 tests** (construyen motores con `__new__`, nunca setean `self.ollama`, y monkeypatchean `ollama.show` global). **Edit C DROPPED.**

## FIX-1 (BFA-app_shell-1) — APLICADO
- `opencohost/ui/app_shell.py:713-714`: borradas 2 líneas duplicadas verbatim de 710-711 (`grid_remove()` idempotente + assignment idéntico). Se conserva el `grid_rowconfigure` único.
- Behavior-preserving. Validado: `test_app_shell_obs_resilience.py`.

## FIX-2 (BFA-llm-8) — APLICADO PARCIAL (A+B, C descartado)
- `opencohost/core/llm_engine.py:1625` (`_first_sentence`) y `:1647` (`_sanitize_history_context`): borrados los `import re` locales (`re` es module-level en :2).
- **Edit C NO aplicado**: el `import ollama`/`ollama.show` en `_fetch_show` (:1489) se deja intacto — el swap rompía tests por construcción `__new__` sin `self.ollama`.
- Validado: `test_context_overflow_guardrail.py`, `test_reasoning_token_budget.py`.

## FIX-3 (BFA-llm-7) — APLICADO (2 archivos)
- Estado muerto `_pending_switch_retries`: grep repo-wide confirmó **0 lecturas** (solo 4 writes prod + 9 refs en test).
- `opencohost/core/llm_engine.py`: borradas las 4 líneas (decl :121, sets :332/:735/:886).
- `tests/test_llm_engine_model_trace.py`: borradas las 9 líneas (7 set-sites + 2 asserts `== 3`).
- Método de borrado: filtro de líneas en Python (elimina toda línea con el token, preservando EOLs) — robusto a la no-unicidad que el juez señaló (el bloque de T5/360 es byte-idéntico al de T2a/205). Verificado: `rg _pending_switch_retries` → 0 restantes.
- Validado: `test_llm_engine_model_trace.py` (22 passed).

## Validación total
`pytest test_llm_engine_model_trace + test_context_overflow_guardrail + test_reasoning_token_budget + test_model_panel + test_app_shell_obs_resilience` → **186 passed**.

## Trazabilidad / rollback
Un commit por concern; `git revert <sha>`. Diseño completo + verdict en el transcript del workflow `wf_eb8f0fde-988`.
