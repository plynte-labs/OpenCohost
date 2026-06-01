# ADR-004: Packaging Readiness Audit

**Date**: 2026-05-29  
**Status**: Proposed  
**Branch**: `audit/packaging-readiness`  
**Author**: Audit Agent

## Context

VoiceAI es una app de desktop con GUI (customtkinter) que depende de Ollama como servicio externo para LLM. Antes de empaquetarla para distribución a usuarios finales, necesitamos identificar qué está listo, qué bloquea, y qué riesgos existen.

### Conexión con el Contexto Histórico del Proyecto

Es fundamental destacar que los dos problemas raíz que guían esta auditoría **no son descubrimientos nuevos**, sino que ya eran dolores conocidos e identificados en el historial de desarrollo del proyecto y documentados en Engram (asociados a sesiones de resiliencia previas como Bugs E, H, L, M):
1.  **La "GodClass" (`ui/app_shell.py`)**: Su tamaño y alta densidad de responsabilidades ya habían sido catalogados como deuda técnica crítica. Esta auditoría aporta métricas empíricas exactas (`assert 3062 < 3000`) aportadas por la suite de integración.
2.  **La falta de aislamiento entre entornos**: El desajuste de dependencias y variables entre el entorno local de desarrollo, el entorno de ejecución de tests y el futuro entorno empaquetado (`sys.frozen`) ya era un foco de fricción constante.

El valor de este documento no reside en descubrir estos problemas, sino en **ponerles números concretos, documentar sus riesgos específicos para el empaquetado binario y proveer un arnés de tests de estrés** que prevenga regresiones cuando se decidan encarar los fixes.

Esta auditoría se basa en código y tests verificados en el repo actual (`f314a0e` HEAD).

## Hallazgos

### 1. No existe manifiesto de dependencias

**Risk: 🔴 CRÍTICO**

No hay `requirements.txt`, `pyproject.toml`, ni `setup.py`. Sin esto, es imposible:
- Reproducir el entorno en otra máquina
- Que PyInstaller/cx_Freeze resuelva dependencias
- Que un CI/CD instale el proyecto

**Evidencia Empírica de Falla**:
Durante la ejecución completa de la suite de tests en la tarea `task-143`, se produjeron múltiples fallas en `tests/test_integration.py` por dependencias faltantes al importar módulos clave de producción:
*   `ModuleNotFoundError: No module named 'soundfile'` en `ui/app_shell.py`
*   `ModuleNotFoundError: No module named 'pynput'` en `ui/ptt_manager.py`

**Dependencias externas detectadas por imports** (no exhaustivo):
`customtkinter`, `ollama`, `requests`, `websocket`, `sounddevice`, `soundfile`, `pygame`, `pyyaml`, `PIL` (Pillow), `pynput`/`keyboard`, `google-auth`, `google-api-python-client`

**Fix**: Crear `pyproject.toml` con secciones `[project.dependencies]` y `[project.optional-dependencies]` para TTS pesado (torch, transformers).

**Archivos a crear**: `pyproject.toml`

---

### 2. `BASE_DIR` usa `__file__` — se rompe empaquetado

**Risk: 🟠 ALTO**

Dos módulos definen `BASE_DIR` con `__file__`:

| Archivo | Línea | Patrón |
|---------|-------|--------|
| `config/settings.py` | 9 | `os.path.dirname(os.path.dirname(os.path.abspath(__file__)))` |
| `config/storage.py` | 20 | `Path(__file__).resolve().parent.parent` |

En PyInstaller con `--onefile`, `__file__` apunta a un directorio temporal (`_MEIPASS`) que se borra al cerrar. Archivos de configuración, modelos, logs, y assets quedan inaccesibles.

No hay ninguna referencia a `sys.frozen`, `sys._MEIPASS`, ni `bundle_dir` en el codebase.

**Fix**: Crear helper `get_app_dir()` que detecte `sys.frozen` y retorne la ruta correcta:
```python
def get_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent
```

**Archivos a modificar**: `config/settings.py`, `config/storage.py`  
**Nota**: No modifiqué estos archivos. Solo tests.

---

### 3. `apply_storage_environment()` se ejecuta al importar

**Risk: 🟠 ALTO**

`config/storage.py` línea 101:
```python
STORAGE_PATHS = apply_storage_environment()
```

Esto ejecuta al importar:
- `os.environ["TEMP"] = ...` — sobreescribe TEMP del sistema
- `os.environ["HF_HOME"] = ...` — sobreescribe cache de HuggingFace
- Crea directorios (`ensure_storage_dirs`)

En un entorno empaquetado, esto puede:
- Crear carpetas en ubicaciones inesperadas
- Contaminar variables de entorno del usuario
- Fallar si no tiene permisos de escritura

**Fix**: Mover a inicialización explícita en `main.py`, no al import.

**Archivos a modificar**: `config/storage.py`, `main.py`

---

### 4. Ollama `serve` arranca sin feedback útil al usuario

**Risk: 🟡 MEDIO** (ya parcialmente mitigado)

Commit `f314a0e` agregó `OllamaStartupManager` que:
- ✅ Guarda process handle
- ✅ Captura stderr a log
- ✅ Timeout configurable (60s)
- ✅ Diagnostica proceso muerto temprano
- ✅ Inyecta `OLLAMA_MODELS` al subproceso

Pero `model_panel.py` línea 527 llama `refresh_ollama_state()` sin distinguir timeout vs éxito parcial en la UI. El usuario ve el mismo estado genérico en ambos casos.

**Fix**: Propagar `OllamaStartupResult.status` a la UI como estado distinguible.

**Archivos a modificar**: `ui/model_panel.py`

---

### 5. Rutas hardcodeadas al disco del proyecto

**Risk: 🟡 MEDIO**

| Archivo | Ruta | Problema empaquetado |
|---------|------|---------------------|
| `config/settings.py:133` | `perfiles.json` en `BASE_DIR` | Archivo de usuario junto al ejecutable |
| `config/settings.py:164-168` | `config/*.json` en `BASE_DIR` | Configuración mutable en directorio de instalación |
| `core/music_library.py:16-17` | `assets/music/` y `config/music_library.json` | Assets y config mezclados |
| `core/cohost_profiles.py:15` | `cohost_profiles.json` en `BASE_DIR` | Archivo de usuario en directorio de app |
| `avatar/avatar_config.py:21-22` | `config/avatar.yaml` y `assets/avatar/kira/` | Config y assets mixtos |

**Fix**: Separar datos de usuario (writable) de assets de app (read-only):
- App dir: ejecutable + assets estáticos
- User data dir: `%APPDATA%/VoiceAI/` para config, perfiles, logs, modelos

**Archivos a modificar**: `config/settings.py`, `config/storage.py`, archivos que usen `BASE_DIR` para datos mutables

---

### 6. `_emergency_cleanup` en `main.py` importa `ollama` at cleanup time

**Risk: 🟡 MEDIO**

`main.py` línea 33:
```python
import ollama
ollama.generate(model=model, prompt="", keep_alive=0)
```

En un crash, el import puede fallar si el módulo no está disponible. `atexit` handlers se ejecutan durante shutdown de Python donde el estado de imports no está garantizado.

**Fix**: Import `ollama` al inicio del módulo con try/except, y en el handler solo usar la referencia cached.

**Archivos a modificar**: `main.py`

---

### 7. No hay test de resolución de rutas para frozen app

**Risk**: 🟡 MEDIO

Los tests no verifican qué pasa cuando `sys.frozen = True` y las rutas se resuelven contra `sys.executable` en vez de `__file__`.

**Fix**: Agregado en esta rama — test parametrizado para storage paths.

---

### 8. Streaming pipeline no maneja texto residual

**Risk**: 🟢 BAJO

`StreamingSpeechPipeline.run()` consume deltas del LLM y los parte en oraciones completas. Si el LLM termina con texto parcial (no termina en `.?!`), ese texto se pierde silenciosamente.

**Fix**: Después del loop de `stream()`, flushear el buffer restante del splitter.

**Archivos a modificar**: `core/streaming_speech.py`, `core/sentence_splitter.py`

---

### 9. `app_shell.py` excede el límite de tamaño de archivo (3000 líneas)

**Risk: 🟡 MEDIO (Mantenibilidad / Deuda Técnica)**

El test de integración `TestAppShellStructure.test_app_shell_line_count_under_1500` falló debido a que `ui/app_shell.py` tiene actualmente **3062 líneas**, excediendo el límite máximo tolerado de 3000 líneas establecido por la suite.

Aunque no bloquea la compilación binaria directamente, un archivo de UI de este volumen introduce un alto acoplamiento, dificulta el análisis estático de PyInstaller para resolver dependencias secundarias y aumenta la probabilidad de dependencias circulares en tiempo de importación (como evidencian los fallos en `test_no_circular_imports` y `test_all_panel_modules_exist`).

**Fix**: Modularizar `app_shell.py`. Delegar la inicialización y el cableado (wiring) de paneles específicos (como el panel de Avatar, Cohost o el de diagnóstico) a clases coordinadoras o builders dedicados para reducir el tamaño del archivo por debajo de 2000 líneas.

**Archivos a modificar**: `ui/app_shell.py`

---

## Decisión: Prioridades para Empaquetado

### P0 — Bloquean empaquetado
1. Crear `pyproject.toml` con dependencias (Hallazgo 1)
2. Implementar `get_app_dir()` con detección `sys.frozen` (Hallazgo 2)

### P1 — Causan bugs en producción empaquetada
3. Mover `apply_storage_environment()` a inicialización explícita (Hallazgo 3)
4. Separar user data de app dir (Hallazgo 5)
5. Fix `_emergency_cleanup` import safety (Hallazgo 6)

### P2 — Mejoran robustez y mantenibilidad
6. Propagar startup status a UI (Hallazgo 4)
7. Flush buffer residual del splitter (Hallazgo 8)
8. Modularizar `app_shell.py` para cumplir con límite de líneas e imports limpios (Hallazgo 9)

## Tests Agregados en Esta Rama

| Test | Qué cubre |
|------|-----------|
| `test_storage_paths_frozen_app` | Verifica que `resolve_storage_paths` funciona con config explícita, simulando un escenario empaquetado |
| `test_storage_paths_custom_disk` | Ollama en otro disco, HF cache en otro disco |
| `test_storage_env_isolation` | `apply_storage_environment` no contamina env vars sin revert |
| `test_splitter_unicode_ellipsis` | Puntos suspensivos Unicode (…) no rompen el splitter |
| `test_splitter_rapid_fire_deltas` | Deltas de 1 char simulando streaming real de LLM |
| `test_splitter_trailing_incomplete` | Texto residual al final del stream |
| `test_pipeline_empty_stream` | LLM no emite nada |
| `test_pipeline_long_burst` | 20+ oraciones en un solo delta |
| `test_startup_popen_raises` | `Popen` falla (ej: Ollama no instalado, permisos) |
| `test_startup_flaky_readiness` | Readiness intermitente: ready→not ready→ready |

## Affected Files (Esta Rama)

| Archivo | Acción | Resumen |
|---------|--------|---------|
| `docs/adr/ADR-004-packaging-readiness.md` | NEW | Este documento |
| `tests/test_sentence_splitter.py` | MODIFY | +4 tests de estrés real |
| `tests/test_streaming_speech_pipeline.py` | MODIFY | +3 tests de escenarios reales |
| `tests/test_ollama_startup.py` | MODIFY | +2 tests de robustez |
| `tests/test_storage_packaging.py` | NEW | Tests de rutas para empaquetado |

## Related

- ADR-002: UI Presentation Refactor
- Commit `f314a0e`: Harden managed startup diagnostics
- Commit `85f5d4a`: Sync Ollama readiness with model intent
- Conductor track: `first_run_readiness_wizard_20260529`
