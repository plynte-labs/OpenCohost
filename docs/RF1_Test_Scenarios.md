# RF1 — Escenarios de Prueba Esperados (PTT)

**Código:** RF1-TEST  
**Módulo:** Control & Push-to-Talk  
**Versión:** 1.0  
**Rama:** `feature/rf1-ptt-hotkey`

---

## 1. Escenarios Positivos

### TEST-PTT-001 — Activar PTT y grabar audio válido

**Precondiciones:**
- VoiceAI ejecutándose.
- Modo PTT = OFF.
- Audio de referencia cargado.

**Pasos:**
1. Activar el switch PTT a ON.
2. Seleccionar `F10` como hotkey.
3. Presionar `F10` y hablar durante 3 segundos.
4. Soltar `F10`.

**Resultado esperado:**
- La UI muestra "GRABANDO..." mientras se mantiene presionado.
- Al soltar, el audio se valida (RMS > 0.005) y se envía al motor IA.
- Kira responde con voz en < 5 segundos.

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
- `F10` no activa la grabación.
- `Mouse4` activa y detiene la grabación correctamente.
- No hay excepciones en consola.

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
- La grabación se realiza en buffer pero **no se envía** inmediatamente.
- Una vez `_speaking == False`, el buffer se envía al motor IA.
- Kira responde al audio capturado sin pérdida.

---

## 2. Escenarios Negativos

### TEST-PTT-004 — Grabar silencio (RMS bajo)

**Precondiciones:**
- PTT = ON.
- Micrófono conectado pero sin hablar.

**Pasos:**
1. Presionar hotkey durante 3 segundos en silencio absoluto.
2. Soltar hotkey.

**Resultado esperado:**
- La consola muestra: `[PTT] Audio descartado: silencio detectado (RMS: X.XXXX)`.
- No se envía nada al motor IA.
- La UI vuelve a estado idle.

---

### TEST-PTT-005 — Presionar hotkey sin PTT activado

**Precondiciones:**
- PTT = OFF.

**Pasos:**
1. Presionar `F10`.

**Resultado esperado:**
- No ocurre nada (la tecla no está interceptada).
- El WebSocket continúa operando normalmente.

---

## 3. Escenarios de Límite y Estrés

### TEST-PTT-006 — Ciclos rápidos de press/release

**Precondiciones:**
- PTT = ON.

**Pasos:**
1. Presionar y soltar el hotkey 20 veces en 10 segundos.

**Resultado esperado:**
- Cada ciclo genera una grabación independiente.
- No hay acumulación de memoria (buffers liberados).
- La UI no se congela.

---

### TEST-PTT-007 — Grabación muy corta (< 0.5 s)

**Precondiciones:**
- PTT = ON.

**Pasos:**
1. Presionar y soltar el hotkey en < 0.3 segundos.

**Resultado esperado:**
- Audio descartado por duración insuficiente.
- Mensaje en consola: `[PTT] Audio descartado: duración insuficiente (X.XXs)`.

---

### TEST-PTT-008 — Grabación muy larga (> 30 s)

**Precondiciones:**
- PTT = ON.

**Pasos:**
1. Mantener presionado el hotkey durante 35 segundos.
2. Soltar.

**Resultado esperado:**
- La grabación se trunca automáticamente a 30 segundos.
- Se procesa el segmento truncado.
- Mensaje de advertencia: `[PTT] Grabación truncada a 30s`.

---

## 4. Matriz de cobertura

| Escenario | RF1.1 | RF1.2 | RNF1.1 | RNF1.2 | RNF1.3 |
|-----------|-------|-------|--------|--------|--------|
| TEST-PTT-001 | ✅ | ✅ | ✅ | ✅ | ✅ |
| TEST-PTT-002 | — | ✅ | ✅ | ✅ | ✅ |
| TEST-PTT-003 | ✅ | ✅ | — | ✅ | — |
| TEST-PTT-004 | — | ✅ | — | ✅ | ✅ |
| TEST-PTT-005 | ✅ | ✅ | — | ✅ | — |
| TEST-PTT-006 | ✅ | ✅ | ✅ | ✅ | ✅ |
| TEST-PTT-007 | — | ✅ | — | ✅ | ✅ |
| TEST-PTT-008 | — | ✅ | — | ✅ | ✅ |

