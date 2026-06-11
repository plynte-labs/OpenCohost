# RF2 — Requerimientos Funcionales: UI/UX y Pipeline Visual

**Código:** RF2  
**Módulo:** Interfaz y Experiencia de Usuario  
**Versión:** 1.0  
**Rama:** `feature/rf2-ui-ux`

---

## 1. Alcance

Optimizar la interfaz gráfica de OpenCohost para entornos de streaming multi-monitor, agregar indicadores visuales claros del estado del pipeline de IA, y preparar un panel de registro de acciones administrativas.

---

## 2. Requerimientos Funcionales

### RF2.1 — Arquitectura Multi-Monitor

| Campo | Descripción |
|-------|-------------|
| **ID** | RF2.1 |
| **Prioridad** | Media |

**Descripción:**  
La ventana de OpenCohost debe poder moverse libremente a un segundo monitor sin degradar rendimiento ni requerir overlays. El sistema actual ya usa CustomTkinter (ventana desktop nativa, sin DirectX/Vulkan), por lo que el objetivo principal es persistir geometría y opcionalmente ofrecer modo compacto.

**Sub-requerimientos:**

- **RF2.1a — Persistencia de geometría:** ✅ Implementado. Guardar posición (x, y) y tamaño (width, height) de la ventana al cerrar, restaurar al abrir. Archivo: `config/window_geometry.json`.
- **RF2.1b — Modo compacto:** ✅ Implementado. Switch en `frame_top` que oculta/muestra `frame_model`, `frame_profile`, `frame_bottom`.
- **RF2.1c — Always on Top:** ❌ No requerido por el usuario.

**Respuestas del usuario:**
- RF2.1b: **Sí** — implementar modo compacto
- RF2.1c: **No** — no se necesita Always on Top

**Archivos afectados:**
- `ui/app.py` — `geometry()`, `on_closing()`.

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
| **Escuchando** | PTT presionado O WebSocket activo recibiendo | `ptt_pressed=True` or `ws_connected=True` |
| **Procesando LLM** | Ollama generando respuesta | `ui_callback("processing")` |
| **Sintetizando Voz** | TTS generando audio | Motor `_speaking=True` |
| **Hablando** | Reproduciendo audio | Motor reproduciendo |
| **Descargando Modelo** | Descarga en progreso | `ui_callback("download_start")` |

**Sub-requerimientos:**

- **RF2.2a — Indicador de pipeline:** ✅ Implementado. Label con texto + color para 7 estados.
- **RF2.2b — Indicador de audio:** ✅ Implementado con animación fake. PTT es gate sobre WebSocket (no captura audio local), por lo que se usa animación simulada.
- **RF2.2c — Barra RMS real:** ✅ Implementado.

**Respuestas del usuario:**
- Estilo: **Texto + color**
- Estados a mostrar: **Todos** (En Espera, Escuchando, Procesando LLM, Sintetizando Voz, Hablando)
- Barra RMS: **Sí** — mostrar nivel mientras está "Escuchando"
- Ubicación: **Esquina superior derecha** (como ahora)

**Archivos afectados:**
- `ui/app.py` — `_on_motor_event`, `_actualizar_pipeline`, `_animar_rms`.

---

### RF2.3 — Registro de Acciones (Console Log)

| Campo | Descripción |
|-------|-------------|
| **ID** | RF2.3 |
| **Prioridad** | Baja (framework) / Alta (si se integra con API) |

**Descripción:**  
Panel dedicado donde Kira confirme acciones administrativas ejecutadas, separado del log general de sistema. Ejemplos: "Título de Twitch actualizado a '...'", "Slow Mode activado por 5 min".

**Sub-requerimientos:**

- **RF2.3a — Panel de acciones:** ✅ Implementado. Tabview con pestana "📝 Kira Acciones" (`consola_acciones`).
- **RF2.3b — API de logging:** ✅ Implementado. Método `_log_accion(msg)` persiste en `logs/acciones.jsonl`.
- **RF2.3c — Mensajes simulados:** ✅ Implementado. Mensajes demo insertados en `_build_ui` cuando no hay historial.
- **RF2.3d — Integración futura:** El panel queda listo para recibir callbacks de RF4.x (Twitch/YouTube API).

**Respuestas del usuario:**
- Separación: **Pestañas** (ya implementado así)
- Contenido: **Mensajes simulados de prueba**
- Tipo: **Todo** (acciones + eventos del sistema + transcripciones)
- Persistencia: **Sí** — guardar en `logs/acciones.jsonl`

**Archivos afectados:**
- `ui/app.py` — `_log_accion`, `consola_acciones`.

---

## 3. Matriz de trazabilidad

| RF | Componente | Archivos |
|----|------------|----------|
| RF2.1 | UI, Config | `ui/app.py`, `config/settings.py` |
| RF2.2 | UI, Motor | `ui/app.py`, `core/llm_engine.py` |
| RF2.3 | UI | `ui/app.py` |

---

## 4. Preguntas — RESPUESTAS REGISTRADAS

| Pregunta | Respuesta |
|----------|-----------|
| RF2.1b Modo compacto | **Sí** |
| RF2.1c Always on Top | **No** |
| RF2.2A Estilo indicador | **Texto + color** |
| RF2.2B Estados a mostrar | **Todos** |
| RF2.2C Barra RMS | **Sí** |
| RF2.2D Ubicación | **Esquina superior derecha** |
| RF2.3A Separación | **Pestañas** |
| RF2.3B Contenido | **Mensajes simulados** |
| RF2.3C Tipo | **Todo** (acciones + eventos + transcripciones) |
| RF2.3D Persistencia | **Sí** |
