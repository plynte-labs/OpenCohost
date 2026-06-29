# 07 — Validation

- **Fecha**: 2026-06-29 · **Branch**: `maintenance/big-file-audit-small-fixes-20260629`
- Intérprete: `E:\Miniconda\envs\flux_env\python.exe` · flags `-p no:cacheprovider --basetemp=E:/VoiceAI/temp/pytest-piper-clean`

## Validado por TEST (pytest)
| Comando | Resultado | Cubre |
|---|---|---|
| `pytest tests/test_launcher.py` (antes del fix) | **1 failed, 84 passed** (RED esperado) | BFA-launcher-4 RED confirmado (strict TDD) |
| `pytest tests/test_launcher.py tests/test_stream_admin_ui.py tests/test_cohost_agenda_panel_tokens.py tests/test_ollama_startup.py tests/test_llm_memory_config.py` (tras fixes) | **219 passed** | BFA-launcher-1/4, BFA-5/6/7, BFA-KAC-8 + regresión base ADR-022/023 |

## Validado por RAZONAMIENTO + check aislado
| ID | Check | Resultado |
|---|---|---|
| BFA-6 | `python -c` con dict de 1003: borra los 3 oldest-inserted, len→1000, los nuevos sobreviven | **OK** (equivalencia con `list(keys())[:excess]` demostrada) |
| BFA-KAC-8 | `"dinamico"` ∈ `RHYTHM_RULES` (:299) ⇒ la rama borrada era no-op | **OK** (provable, + suite verde) |
| BFA-5 | `import threading` module-level en :29 ⇒ borrar el local es seguro | **OK** (+ import-clean en suite) |
| BFA-launcher-1 | `reporter.detail()` existe en ambos reporters (:989 Tk, :1094 Headless) | **OK** (verificado por grep) |

## Pendiente de RUNTIME MANUAL (no afirmado como validado)
- **BFA-launcher-1**: confirmar visualmente — parar Ollama → bootstrap GUI → abrir "Details ▸" → el NOTE persiste. (El test cubre la API; el render es runtime.)
- Nada más requiere runtime para los fixes aplicados.

## Lint/typecheck
No se corrió linter/typecheck dedicado en esta tanda (no hay gate configurado en el flujo; los cambios son mínimos y la suite cubre import-cleanliness).

## Separación de garantías (Fase 9)
- **Por test**: BFA-launcher-1/4, BFA-5/6/7, BFA-KAC-8 (suite verde) + BFA-launcher-4 (RED→GREEN).
- **Por razonamiento/check aislado**: BFA-6 (algoritmo), BFA-KAC-8 (no-op).
- **Por inspección** (diferidos, no aplicados): BFA-app_shell-1, BFA-llm-8, BFA-llm-7.
- **Pendiente runtime manual**: BFA-launcher-1 (render del panel).
