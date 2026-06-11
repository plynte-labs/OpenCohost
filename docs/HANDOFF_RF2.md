# Handoff — Preparación RF2.1 / RF2.2 / RF2.3 para Agente Implementador

**Fecha:** 2026-05-04  
**Rama de trabajo:** `feature/rf2-ui-ux`  
**Ambiente de ejecución:** `python main.py` (activate your project Python environment first)

---

## 1. Contexto actual

OpenCohost tiene una UI funcional pero básica. Los indicadores de estado son limitados (solo texto), no hay persistencia de geometría de ventana, y no hay panel separado para acciones administrativas.

### Lo que YA existe y se puede reutilizar
- `lbl_status` en `frame_top`: muestra `⏳ Inicializando...`, `✅ Listo`, `🔄 Procesando...`, `📥 Descargando modelo...`
- `self._on_motor_event(status)`: dispatch central de estados del motor
- `consola` (`CTkTextbox`): log general de sistema
- `ptt_pressed`, `ws_connected`: flags ya disponibles
- `config/ptt_settings.json`: precedente de persistencia JSON

---

## 2. Preguntas para refinar RF2.1, RF2.2, RF2.3

> **Responde solo lo necesario. Si algo no te importa, se usa el default razonable.**

### RF2.1 — Multi-Monitor

Lo minimo es guardar/restaurar posicion de ventana. Lo demas es opcional:

**A) Persistencia de geometria**
- [ ] Si — guardar (x, y, width, height) al cerrar y restaurar al abrir

**B) Modo compacto**
- [ ] Si — un boton para alternar vista completa/reducida (solo consola + estado)
- [ ] No — dejar el tamaño fijo como ahora

**C) Always on Top**
- [ ] Si — checkbox "Siempre visible"
- [ ] No — no necesito

**D) ¿Algo mas especifico que necesites para multi-monitor?**

---

### RF2.2 — Pipeline Visual

Actualmente el label de estado muestra texto simple. Las opciones:

**A) Estilo del indicador**
- [ ] Texto + color (ej: verde=escuchando, amarillo=procesando, azul=sintetizando)
- [ ] Solo colores (tipo semaforo: circulos verde/amarillo/rojo)
- [ ] Barra de pasos (1-2-3-4 con el paso actual resaltado)

**B) Estados a mostrar** (marca los que quieras)
- [ ] En Espera (idle)
- [ ] Escuchando (PTT presionado o WS activo)
- [ ] Procesando LLM (Ollama pensando)
- [ ] Sintetizando Voz (TTS generando)
- [ ] Hablando (reproduciendo audio)

**C) Nivel de audio**
- [ ] Mostrar barra/nivel RMS mientras se esta en "Escuchando"
- [ ] No, solo el indicador de estado

**D) ¿Ubicacion del indicador?**
- [ ] Mismo lugar que ahora (esquina superior derecha)
- [ ] Barra inferior tipo "status bar"
- [ ] Otra: ___

---

### RF2.3 — Panel de Acciones

Actualmente hay 1 sola consola. Opciones:

**A) Separacion**
- [ ] Panel separado (2do CTkTextbox) solo para "Kira confirma X"
- [ ] Misma consola pero con prefijo [ACCION] para filtrar visualmente
- [ ] Pestanas (tab) para cambiar entre Log General / Acciones

**B) Contenido hoy** (sin API de Twitch aun)
- [ ] Mensajes simulados dummies para probar el panel (ej: "[Kira] Titulo actualizado (simulado)")
- [ ] Solo preparar la estructura, sin mensajes aun
- [ ] Integrar algo basico ya (¿que API?)

**C) ¿Que tipo de acciones deberia registrar este panel?**
- [ ] Solo acciones de Kira (cambios que ella hace al stream)
- [ ] Tambien eventos del sistema (modelo cambiado, WS conectado, etc.)
- [ ] Todo: acciones + eventos + transcripciones

**D) ¿Necesitas que el panel de acciones sea persistente (guarde historial en disco)?**
- [ ] Si — guardar en `logs/acciones.jsonl`
- [ ] No — solo en memoria durante la sesion

---

## 3. Plan de implementacion tentativo

| Paso | RF | Cambio | Complejidad |
|------|-----|--------|-------------|
| 1 | RF2.2 | Mejorar `_on_motor_event` + `_set_ptt_status` para pipeline visual | Baja |
| 2 | RF2.1 | Persistir geometria en `config/window_geometry.json` | Baja |
| 3 | RF2.3 | Agregar panel de acciones (2do textbox o seccion) | Media |
| 4 | RF2.1 | (opcional) Modo compacto / Always on Top | Baja |

---

## 4. Checklist

- [x] PR #2 mergeado a master
- [x] Rama `feature/rf2-ui-ux` creada
- [x] Docs funcionales creados
- [x] Docs de calidad creados
- [x] **Respuestas del usuario recibidas:** RF2.1b=Si, RF2.2=Texto+color+Todos+RMS+Ubicacion actual, RF2.3=Pestanas+Mensajes+Todo+Guardar
- [x] **Implementacion completada:** Pipeline visual con 7 estados, barra RMS real, modo compacto, mensajes simulados en panel acciones
