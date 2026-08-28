# ADR-052: Retiro Formal y Eliminacion de la UI Legacy CustomTkinter (Track legacy_ctk_ui_retirement_20260828)

**Fecha**: 2026-08-28  
**Estado**: Aceptado / Implementado  
**Branch**: cleanup/retire-legacy-ctk-ui  
**Track**: legacy_ctk_ui_retirement_20260828  
**Commits**:
- WU1: 598e91f (refactor(ui): sever legacy CTk entrypoint in __main__.py with explicit retirement notice)
- WU2: 1eecab7 (refactor(ui): remove legacy custom-tkinter UI and decouple preserved test suite)
- WU3: 1f4d0ce (chore(deps): remove customtkinter dependency and legacy packaging references)

---

## 1. Contexto y Diagnostico

Desde la introduccion de la arquitectura moderna de OpenCohost v2 (ADR-038..051), la interfaz de usuario oficial es la aplicacion de escritorio basada en **Tauri + React + Tailwind** (OpenCohost_UI/), conectada al backend desacoplado via FastAPI (opencohost.api.main / EngineHost).

El paquete original opencohost/ui/ basado en CustomTkinter (34 archivos, 12,044 LOC) se encontraba congelado como legacy de solo lectura. Sin embargo, su presencia activa en el arbol de codigo acarreaba:
1. **Ruido visual y cognitivo severo**: 43 suites de tests exclusivas de widgets CTk requerian mantenimiento continuo ante cada refactorizacion del motor.
2. **Dependencias obsoletas en el lockfile**: ~112 paquetes de Python (customtkinter, pillow, pynput, keyboard, google-api-python-client, sounddevice, soundfile, etc.) eran innecesariamente resueltos y empaquetados.
3. **Ambiguedad de entrypoints**: python -m opencohost intentaba levantar la aplicacion CTk en lugar de orientar al usuario hacia los entrypoints canonicos.

---

## 2. Decisiones Arquitectonicas

1. **Retiro fisico y preservacion en Git**: El historial de Git es el archivo historico canonico del proyecto. No se crean carpetas artificiales como /legacy; se elimina opencohost/ui/ limpiamente.
2. **Deprecacion explicita de __main__.py**: python -m opencohost no se redirige silenciosamente a FastAPI ni a Tauri. Emite un aviso informativo y sale de inmediato con codigo 0, indicando los entrypoints soportados (opencohost-api y OpenCohost).
3. **Preservacion estricta de contratos de backend**: Antes de la eliminacion de archivos UI, todos los contratos compartidos (PTT headless, health monitor, audio bed off-thread, chat reaction history locks, agenda queue, y profile UUID persistence) fueron desacoplados de fixtures CTk y verificados al 100% en modo puro.
4. **Purga de dependencias**: Se elimino el grupo legacy-ui de pyproject.toml, purgando customtkinter y todas sus dependencias transitivas de uv.lock.

---

## 3. Desglose de Work Units y Metricas

### WU1: Desconexion de Entrypoints (598e91f)
- Se sustituyo opencohost/__main__.py por un script liviano sin dependencias externas (sys unicamente).
- Cero importaciones de opencohost.ui en codigo productivo.

### WU2: Migracion de Tests Compartidos y Eliminacion Fisica (1eecab7)
- **Archivos eliminados**: 34 archivos de codigo productivo en opencohost/ui/ + 43 archivos de tests exclusivos de widgets CTk.
- **Tests compartidos preservados**:
  - tests/test_api_ptt.py: contratos de PTT headless y privacidad de transcripcion.
  - tests/test_health_integration.py: fallback automatico por VRAM degradada.
  - tests/test_audio_bed_offthread.py: concurrencia y decodificacion off-lock de audio bed.
  - tests/test_kira_chaos_stream.py & tests/test_kira_orchestration_gaps.py: integracion de agenda y streaming sin dependencias de AppShell.
  - tests/test_memorias_profile_uuid.py: persistencia atomica, de-duplicacion determinista y ciclo de vida de UUID bajo _history_lock.
- **Metrica del commit**: **88 files changed, 106 insertions(+), 40,884 deletions(-)**.

### WU3: Limpieza de Dependencias y Packaging (1f4d0ce)
- Eliminacion del extra legacy-ui en pyproject.toml.
- Sincronizacion de uv.lock: **112 dependencias innecesarias removidas**.
- Actualizacion de .github/workflows/ci.yml y tests/test_packaging_core_boundary.py para asegurar que customtkinter jamas ingrese a la distribucion headless/Tauri.
- **Metrica del commit**: **4 files changed, 90 insertions(+), 3,699 deletions(-)**.

---

## 4. Evidencia y Verificacion

### Backend Pytest Suite
`	ext
4,379 passed, 13 skipped, 4 warnings in 270s (0:04:30)
Fallos: 0
`

### Frontend Vitest Suite (OpenCohost_UI)
`	ext
Test Files: 90 passed (90)
Tests:      1,206 passed (1,206)
Fallos:     0
`

### Revision Adversarial Dual (Judgment Day)
- **Judge A (Packaging & Runtime Isolation)**: APPROVED (0 blockers). Verifico aislamiento de importaciones, ausencia de dependencias ocultas y alineacion estricta de CI.
- **Judge B (Test Legitimacy & Backend Preservation)**: APPROVED (0 blockers). Verifico legitimidad de aserciones en los 6 subsistemas criticos y confirmo que ninguna prueba de backend fue omitida o enmascarada.

---

## 5. Consecuencias

- **Positivas**:
  - Eliminacion de **44,583 lineas** de codigo y tests obsoletos del espacio de trabajo.
  - Aislamiento total del runtime productivo (FastAPI + Tauri).
  - Reduccion drastica del tiempo de resolucion y tamano del lockfile.
  - Mantenimiento enfocado exclusivamente en la arquitectura moderna de OpenCohost v2.
- **Negativas**: Ninguna. La interfaz Tauri se encuentra 100% funcional y probada.
