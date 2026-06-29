# 06 — Applied Fixes

- **Fecha**: 2026-06-29 · **Branch**: `maintenance/big-file-audit-small-fixes-20260629` · **Commit base**: `6d190b1`
- 6 fixes aplicados en 3 work-unit commits. Todos no-core. Diff total chico, reversible.

## Commit `02f36c0` — fix(launcher)
- **BFA-launcher-4** (bug): `installed_version_satisfies` ahora paddea ambas tuplas a igual aridad → `"1.2"` satisface `"1.2.0"`. Antes `(1,2) >= (1,2,0)` era False → re-bootstrap espurio. **strict TDD**: 3 tests nuevos (RED confirmado antes del fix, GREEN después).
- **BFA-launcher-1** (additive): `reporter.detail("NOTE: %s" % ollama_status.message)` tras el `notify()` para que el warning de Ollama sobreviva en el panel Details (el `notify()` lo pisaba el status tick de 50ms). Ambos reporters exponen `detail()` (verificado :989/:1094).
- Archivos: `packaging/launcher.py`, `tests/test_launcher.py`.

## Commit `b57d6d3` — refactor(stream-admin)
- **BFA-5**: `worker()` deja `func()` (borra `result` no usado + `if result is not None: pass`); borra `import threading` local (module-level en :29).
- **BFA-6**: eviction de `_chat_users` con `for _ in range(excess): del self._chat_users[next(iter(self._chat_users))]` en vez de construir `list(keys())[:excess]`. Hot path por-mensaje bajo `_chat_lock`. Behavior-preserving (mismos víctimas oldest-inserted).
- **BFA-7**: `tags = [t.strip() for t in tags_entry.get().split(",") if t.strip()] if tags_entry else []` (colapsa el dual-typing + isinstance).
- Archivo: `opencohost/ui/stream_admin_ui.py`.

## Commit `35bb610` — refactor(agenda)
- **BFA-KAC-8**: borra `if normalized == "dinamico": return "dinamico"` (no-op — `"dinamico"` ∈ `RHYTHM_RULES`, la línea siguiente ya lo cubre).
- Archivo: `opencohost/smart_aggregator/kira_agenda_controller.py`.

## NO aplicado (diferido a OK del owner)
BFA-app_shell-1, BFA-llm-8, BFA-llm-7 (ver `05_fix_candidates.md`). Ningún archivo core fue modificado en esta tanda.

## `git diff --stat` (rama vs checkpoint 6d190b1)
```
packaging/launcher.py                              | ~10 +-
tests/test_launcher.py                             | +12
opencohost/ui/stream_admin_ui.py                   | ~8 +-
opencohost/smart_aggregator/kira_agenda_controller.py | -2
```
