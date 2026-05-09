# RF1 — Escenarios de Prueba Esperados (PTT)

**Código:** RF1-TEST  
**Módulo:** Control & Push-to-Talk  
**Versión:** 1.0  
**Rama:** `feature/rf1-ptt-hotkey`

---

## 1. Escenarios Positivos

### TEST-PTT-001 — Activar PTT y recibir transcripción vía WebSocket

**Precondiciones:**
- VoiceAI ejecutándose y conectada a LiveAudio (WebSocket).
- Modo PTT = OFF.
- Audio de referencia cargado.

**Pasos:**
1. Activar el switch PTT a ON.
2. Seleccionar `F10` como hotkey.
3. Presionar `F10` y hablar al micrófono de LiveAudio.
4. Soltar `F10`.

**Resultado esperado:**
- La UI muestra "ESCUCHANDO..." mientras se mantiene presionado. ✅ Implementado
- Las transcripciones de LiveAudio se descartan mientras PTT está activo sin presionar. ✅ Implementado
- Al presionar hotkey y hablar, las transcripciones se aceptan. ✅ Implementado
- Kira responde con voz en < 5 segundos. ✅ (flujo normal WebSocket)

**Estado actual:** ✅ PASS — PTT como gate sobre transcripciones WebSocket

---

### TEST-PTT-002 — Cambio de hotkey en caliente

**Precondiciones:**
- PTT = ON.
- Hotkey actual = `F10`.

**Pasos:**
1. Cambiar el hotkey a `Mouse4` desde el dropdown.
2. Presionar `F10`.
3. Presionar `Mouse4` y hablar 2 segundos.
4. Soltar `Mouse4`.

**Resultado esperado:**
- `F10` no activa la grabación (no se abre el gate). ✅
- `Mouse4` activa y desactiva la grabación correctamente. ✅
- No hay excepciones en consola. ✅

**Estado actual:** ✅ PASS

---

### TEST-PTT-003 — Modo half-duplex (esperar a que termine de hablar)

**Precondiciones:**
- PTT = ON.
- Kira está generando/reproduciendo una respuesta larga (`_speaking == True`).

**Pasos:**
1. Presionar el hotkey durante 2 segundos mientras Kira habla.
2. Soltar el hotkey antes de que termine la reproducción.
3. Esperar a que Kira termine de hablar.

**Resultado esperado:**
- Las transcripciones se aceptan en buffer pero se encolan. ✅
- Una vez `_speaking == False`, se procesan las transcripciones encoladas. ✅
- Kira responde sin pérdida. ✅

**Estado actual:** ✅ PASS

---

## 2. Escenarios Negativos

### TEST-PTT-004 — Presionar hotkey sin hablar

**Precondiciones:**
- PTT = ON.
- LiveAudio conectado.

**Pasos:**
1. Presionar hotkey durante 3 segundos sin hablar al mic de LiveAudio.

**Resultado esperado:**
- La UI muestra "ESCUCHANDO..." mientras se presiona. ✅
- Si LiveAudio envía transcripción vacía, se descarta. ✅
- La UI vuelve a estado idle al soltar. ✅

**Estado actual:** ✅ PASS

---

### TEST-PTT-005 — Presionar hotkey sin PTT activado

**Precondiciones:**
- PTT = OFF.

**Pasos:**
1. Presionar `F10`.

**Resultado esperado:**
- No ocurre nada (la tecla no está interceptada para gate). ✅
- El WebSocket continúa operando normalmente. ✅

**Estado actual:** ✅ PASS

---

## 3. Escenarios de Límite y Estrés

### TEST-PTT-006 — Ciclos rápidos de press/release

**Precondiciones:**
- PTT = ON.
- LiveAudio conectado.

**Pasos:**
1. Presionar y soltar el hotkey 20 veces en 10 segundos.

**Resultado esperado:**
- El gate se abre/cierra correctamente en cada ciclo. ✅
- No hay acumulación de memoria. ✅
- La UI no se congela. ✅

**Estado actual:** ✅ PASS

---

### TEST-PTT-007 — Presionar hotkey brevemente

**Precondiciones:**
- PTT = ON.
- LiveAudio conectado.

**Pasos:**
1. Presionar y soltar el hotkey en < 0.3 segundos.

**Resultado esperado:**
- El gate se cierra inmediatamente. ✅
- Si no hay transcripción, no se procesa nada. ✅

**Estado actual:** ✅ PASS

---

### TEST-PTT-008 — Mantener hotkey presionado por largo tiempo

**Precondiciones:**
- PTT = ON.
- LiveAudio conectado.

**Pasos:**
1. Mantener presionado el hotkey durante 60 segundos.
2. Soltar.

**Resultado esperado:**
- El gate permanece abierto mientras se presiona. ✅
- Las transcripciones se procesan continuamente. ✅
- No hay truncado (el límite lo maneja LiveAudio/Whisper). ✅

**Estado actual:** ✅ PASS

---

## 4. Matriz de cobertura

| Escenario | RF1.1 | RF1.2 | RNF1.1 | RNF1.2 | RNF1.3 |
|-----------|-------|-------|--------|--------|--------|
| TEST-PTT-001 | ✅ | ✅ | — | ✅ | ✅ |
| TEST-PTT-002 | ✅ | ✅ | — | ✅ | ✅ |
| TEST-PTT-003 | ✅ | ✅ | — | ✅ | ✅ |
| TEST-PTT-004 | — | ✅ | — | ✅ | ✅ |
| TEST-PTT-005 | ✅ | ✅ | — | ✅ | — |
| TEST-PTT-006 | ✅ | ✅ | — | ✅ | ✅ |
| TEST-PTT-007 | — | ✅ | — | ✅ | ✅ |
| TEST-PTT-008 | — | ✅ | — | ✅ | ✅ |

**Leyenda:** ✅ Pass | — No aplica

