# Handoff — Preparación RF1.1 / RF1.2 para Agente Implementador

**Fecha:** 2026-05-04  
**Rama de trabajo:** `feature/rf1-ptt-hotkey`  
**Responsable de preparación:** Agente OpenCode  
**Ambiente de ejecución:** `python main.py` (activate your project Python environment first)

---

## 1. Estado del entorno

### ✅ Dependencia instalada
- **Librería:** `pynput` v1.8.1
- **Entorno:** `flux_env` (compartido con otros proyectos)
- **Método:** `pip install pynput --no-deps` (sin degradar dependencias existentes)
- **Verificación:** Todos los paquetes críticos intactos:
  - `customtkinter`, `sounddevice`, `websockets`, `ollama`, `pygame`, `numpy`, `requests`, `soundfile`

### ⚠️ Regla de oro para este entorno
> **NO instalar, actualizar ni eliminar paquetes globales sin verificar compatibilidad.**  
> Si se requiere una nueva dependencia, usar `pip install <pkg> --no-deps` y verificar imports críticos.

---

## 2. Documentos de requerimientos creados

| Archivo | Contenido |
|---------|-----------|
| `docs/RF1_Functional_Requirements.md` | RF1.1 (Toggle PTT) y RF1.2 (Hotkey remap) con descripción, reglas de negocio, dependencias y matriz de trazabilidad. |
| `docs/RF1_Quality_Requirements.md` | Requerimientos no funcionales: latencia < 50 ms, robustez (daemon thread), usabilidad (indicadores visuales), compatibilidad Windows. Criterios de aceptación (Definition of Done). |
| `docs/RF1_Test_Scenarios.md` | 8 escenarios de prueba: 3 positivos, 2 negativos, 3 de límite/estrés. Matriz de cobertura vs requerimientos. |
| `docs/changes.md` (actualizado) | Especificación técnica de implementación con notas de rama. |

---

## 3. Archivos a modificar (guía para el implementador)

### `ui/app.py`
- Agregar `CTkSwitch` para PTT toggle (frame de control superior).
- Agregar `CTkOptionMenu` para selección de hotkey (F1–F12, Mouse4, Mouse5, etc.).
- Iniciar/detener `pynput.keyboard.Listener` y `pynput.mouse.Listener` en hilos daemon.
- Implementar `on_press` / `on_release` para captura de audio vía `sounddevice`.
- Integrar lógica de half-duplex: si `_speaking == True`, encolar buffer hasta que termine.
- Validar audio (RMS mínimo, duración) antes de enviar a `command_queue`.

### `core/llm_engine.py`
- Exponer flag `_speaking` de forma thread-safe para consulta desde la UI.
- (Opcional) Agregar comando `"ptt_buffer"` para recibir audio del PTT.

### `config/settings.py`
- Agregar constantes: `PTT_DEFAULT_HOTKEY`, `PTT_MIN_DURATION`, `PTT_MAX_DURATION`, `PTT_RMS_THRESHOLD`.

---

## 4. Decisiones de arquitectura ya tomadas

1. **Modo PTT vs Modo Continuo:** El switch togglea entre ambos. No son mutuamente excluyentes a nivel de código, pero a nivel de UX sí: si PTT=ON, el WebSocket se pausa (descarta transcripciones).
2. **Hilo daemon:** El listener de `pynput` **debe** ser `daemon=True` para no bloquear el cierre de la app.
3. **Grabación en memoria:** Usar `sd.rec(...)` con buffer en memoria, guardar a disco solo si pasa validación.
4. **Half-duplex:** La validación y envío del buffer ocurre en `on_release`, pero si `_speaking` está activo, se encola y se procesa cuando termine.

---

## 5. Checklist de entrega

- [x] `pynput` instalado y verificado en `flux_env`
- [x] Entorno intacto (sin degradaciones)
- [x] Documentos funcionales creados
- [x] Documentos de calidad creados
- [x] Escenarios de prueba definidos
- [x] `changes.md` actualizado con notas técnicas
- [x] **Implementación completada:** UI con switch PTT, hotkey remap, captura de audio via InputStream, validacion RMS/duracion, half-duplex

