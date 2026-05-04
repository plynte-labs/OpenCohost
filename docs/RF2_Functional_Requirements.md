# RF2 — Requerimientos Funcionales: UI/UX y Pipeline Visual

**Código:** RF2  
**Módulo:** Interfaz y Experiencia de Usuario  
**Versión:** 1.0  
**Rama:** `feature/rf2-ui-ux`

---

## 1. Alcance

Optimizar la interfaz gráfica de VoiceAI para entornos de streaming multi-monitor, agregar indicadores visuales claros del estado del pipeline de IA, y preparar un panel de registro de acciones administrativas.

---

## 2. Requerimientos Funcionales

### RF2.1 — Arquitectura Multi-Monitor

| Campo | Descripción |
|-------|-------------|
| **ID** | RF2.1 |
| **Prioridad** | Media |

**Descripción:**  
La ventana de VoiceAI debe poder moverse libremente a un segundo monitor sin degradar rendimiento ni requerir overlays. El sistema actual ya usa CustomTkinter (ventana desktop nativa, sin DirectX/Vulkan), por lo que el objetivo principal es persistir geometría y opcionalmente ofrecer modo compacto.

**Sub-requerimientos propuestos:**

- **RF2.1a — Persistencia de geometría:** Guardar posición (x, y) y tamaño (width, height) de la ventana al cerrar, restaurar al abrir. Archivo: `config/window_geometry.json`.
- **RF2.1b — Modo compacto:** Alternar entre vista completa y vista reducida (solo consola + indicador de estado + PTT), para minimizar espacio en monitor principal.
- **RF2.1c — Always on Top:** Opción para mantener la ventana siempre visible sobre otras aplicaciones.

**Archivos afectados:**
- `ui/app.py` — `geometry()`, `wm_attributes()`, `on_closing()`.

---

### RF2.2 — Estado del Pipeline

| Campo | Descripción |
|-------|-------------|
| **ID** | RF2.2 |
| **Prioridad** | Alta |

**Descripción:**  
La UI debe mostrar indicadores visuales del estado actual del pipeline de procesamiento. Actualmente existe `lbl_status` con estados básicos (`⏳ Inicializando...`, `✅ Listo`, `🔄 Procesando...`). Se necesita granularidad:

| Estado | Significado | Trigger |
|--------|-------------|---------|
| **En Espera** | Sistema idle, sin actividad | `ui_callback("idle")` |
| **Escuchando** | PTT presionado O WebSocket activo recibiendo | `ptt_pressed=True` o `ws_connected=True` |
| **Procesando LLM** | Ollama generando respuesta | `ui_callback("processing")` |
| **Sintetizando Voz** | TTS generando audio | Motor `_speaking=True` |

**Sub-requerimientos propuestos:**

- **RF2.2a — Indicador de pipeline:** Reemplazar el label de estado actual por una barra con 4 estados visuales (texto + color).
- **RF2.2b — Indicador de audio:** Mostrar nivel de audio en tiempo real (RMS en barra) cuando está en modo "Escuchando".

**Archivos afectados:**
- `ui/app.py` — `_on_motor_event`, `_set_ptt_status`.
- `core/llm_engine.py` — `_hablar` (agregar callback `"speaking_start"` / `"speaking_end"`).

---

### RF2.3 — Registro de Acciones (Console Log)

| Campo | Descripción |
|-------|-------------|
| **ID** | RF2.3 |
| **Prioridad** | Baja (framework) / Alta (si se integra con API) |

**Descripción:**  
Panel dedicado donde Kira confirme acciones administrativas ejecutadas, separado del log general de sistema. Ejemplos: "Título de Twitch actualizado a '...'", "Slow Mode activado por 5 min".

**Sub-requerimientos propuestos:**

- **RF2.3a — Panel de acciones:** `CTkTextbox` o `CTkScrollableFrame` separado, solo para mensajes de acción confirmada.
- **RF2.3b — API de logging:** Método `_log_accion(msg)` que publique en el panel de acciones y en el log general.
- **RF2.3c — Integración futura:** El panel queda listo para recibir callbacks de RF4.x (Twitch/YouTube API).

**Archivos afectados:**
- `ui/app.py` — nuevo panel en la UI.
- `core/llm_engine.py` — opcional: nuevo comando `"log_action"`.

---

## 3. Matriz de trazabilidad

| RF | Componente | Archivos |
|----|------------|----------|
| RF2.1 | UI, Config | `ui/app.py`, `config/settings.py` |
| RF2.2 | UI, Motor | `ui/app.py`, `core/llm_engine.py` |
| RF2.3 | UI | `ui/app.py` |

---

## 4. Preguntas abiertas para el usuario

Ver sección 5 del documento `HANDOFF_RF2.md`.
