# RF1 — Requerimientos Funcionales: Módulo de Control de Entrada (PTT)

**Código:** RF1  
**Módulo:** Control & Push-to-Talk  
**Versión:** 1.0  
**Rama:** `feature/rf1-ptt-hotkey`  
**Fecha:** 2026-05-04

---

## 1. Alcance

Este documento define los requerimientos funcionales para el subsistema de **Push-to-Talk (PTT)** de VoiceAI. Su propósito es sustituir la escucha continua por un sistema de captura bajo demanda, eliminando falsos positivos y reduciendo la carga computacional durante sesiones de streaming/gaming.

---

## 2. Requerimientos Funcionales

### RF1.1 — Gestión de Feature Toggle (PTT Master Switch)

| Campo | Descripción |
|-------|-------------|
| **ID** | RF1.1 |
| **Nombre** | Interruptor maestro Push-to-Talk |
| **Prioridad** | Alta |
| **Actor** | Usuario (Streamer) |

**Descripción:**  
La interfaz gráfica debe incluir un control tipo switch (`CTkSwitch`) que permita activar o desactivar globalmente la función de Push-to-Talk.

**Comportamiento:**
- **OFF (default):** El sistema opera en modo continuo (comportamiento actual). El WebSocket de LiveAudio permanece activo y recibe transcripciones en tiempo real.
- **ON:** El sistema entra en modo PTT. El WebSocket se pausa (no se desconecta, pero las transcripciones entrantes se descartan). La captura de audio solo se realiza cuando el usuario mantiene presionada la tecla configurada.

**Reglas de negocio:**
1. El cambio de modo debe ser inmediato (< 100 ms) y no requerir reinicio de la aplicación.
2. Si el motor TTS está hablando (`_speaking == True`), el toggle debe estar deshabilitado para evitar cambios de modo durante la reproducción.
3. El estado del toggle debe visualizarse claramente en la UI (color + etiqueta).

**Dependencias:**
- `ui/app.py` — agregar widget.
- `core/llm_engine.py` — respetar flag `_speaking`.

---

### RF1.2 — Remapeo de Teclas (Global Hotkey)

| Campo | Descripción |
|-------|-------------|
| **ID** | RF1.2 |
| **Nombre** | Configuración de hotkey global PTT |
| **Prioridad** | Alta |
| **Actor** | Usuario (Streamer) |

**Descripción:**  
El usuario debe poder configurar una tecla o botón de mouse que actúe como disparador del PTT, detectable globalmente (incluso cuando la ventana de VoiceAI no tiene el foco).

**Comportamiento:**
- Un dropdown en la UI lista las teclas predefinidas: `F1`–`F12`, `Mouse4`, `Mouse5`, `ScrollLock`, `Insert`.
- Al seleccionar una tecla, el sistema inicia un listener global en un hilo daemon usando `pynput`.
- `on_press`: inicia la grabación de audio (float32, 24000 Hz, mono) en un buffer en memoria.
- `on_release`: detiene la grabación, valida el buffer (RMS mínimo, duración > 0.5 s) y lo envía al motor IA como comando `process_context`.

**Reglas de negocio:**
1. El listener debe correr en un hilo independiente del mixer de pygame para evitar latencia en la detección.
2. Si el motor TTS está hablando (`_speaking == True`), el evento `on_release` debe encolar el buffer y procesarlo **solo** cuando `_speaking` pase a `False` (half-duplex).
3. El listener debe ser detenido y recreado cada vez que se cambia la tecla configurada.
4. No debe capturar la tecla si la aplicación está minimizada o en segundo plano (comportamiento global esperado).

**Dependencias:**
- Librería `pynput` (ya instalada en `flux_env`).
- `sounddevice` para captura de audio bajo demanda.

---

## 3. Matriz de trazabilidad

| RF | Componente afectado | Archivos |
|----|---------------------|----------|
| RF1.1 | UI | `ui/app.py` |
| RF1.1 | Motor de estados | `core/llm_engine.py` |
| RF1.2 | UI + Input Global | `ui/app.py` |
| RF1.2 | Captura de audio | `ui/app.py`, `core/llm_engine.py` |

---

## 4. Glosario

- **PTT:** Push-to-Talk. Sistema de captura de audio bajo demanda mediante una tecla.
- **Half-Duplex:** El sistema no puede escuchar y hablar simultáneamente; la captura se retrasa hasta que termina la reproducción de audio.
- **RMS:** Root Mean Square. Medida de energía del audio usada para detectar silencio.
