# Troubleshooting — VoiceAI

Bugs conocidos, su causa raíz, y cómo se resolvieron. Para que el futuro yo (o cualquier agente) no pierda tiempo reinventando la rueda.

---

## TSH-001: Kira se ponía a hablar sola (Loop de Alucinaciones)

**Síntoma:** Kira empezaba a hablar sin que nadie le hablara, generando respuestas basadas en ruido.

**Causa raíz:** Whisper transcribe ruido de fondo o su propio audio de salida (feedback del altavoz al micrófono) como texto repetitivo: "gracias gracias gracias gracias...". Esto se envía al LLM como input válido.

**Fix (4 capas):**
1. **PTT gate** (`ui/ptt_manager.py`) — Solo captura audio cuando se presiona tecla
2. **Silero VAD** — Descarta segmentos sin voz humana
3. **Anti-loop filter** (`ui/voice_control.py`) — Detecta palabras repetidas >3 veces consecutivas y las deduplica
4. **Half-duplex** (`core/llm_engine.py`) — Bloquea input mientras `_speaking == True`

**Verificación:** Probar con altavoces al máximo y micrófono sensible. Kira no debe activarse sin PTT.

---

## TSH-002: RuntimeError — main thread is not in main loop

**Síntoma:**
```
RuntimeError: main thread is not in main loop
  File "tkinter/__init__.py", line 1557, in _register
    self.tk.createcommand(name, f)
```

**Causa raíz:** El hilo `MotorVocalIA` arrancaba en `__init__` antes de que `app.mainloop()` se ejecutara. Cuando `_check_ollama_service()` terminaba rápido (Ollama ya corriendo), llamaba `ui_callback("ready")` → `self.after(0, ...)`. Tkinter's `after()` requiere mainloop activo.

**Fix:** `ui/app_shell.py` — Cambiar `self.motor_ia.start()` por `self.after(100, self._start_motor)`.

**Verificación:** El log no debe mostrar el error al iniciar la app con Ollama corriendo.

---

## TSH-003: pytchat — signal only works in main thread

**Síntoma:**
```
ValueError: signal only works in main thread
```

**Causa raíz:** `pytchat` internamente usa `signal.signal()` para manejar interrupciones, lo cual solo funciona en el thread principal.

**Fix:** `smart_aggregator/chat_source.py` — Configurar `pytchat.create(..., interruptable=False)` desde `YouTubeChatSource`. También: `source.interruptable: false` en `config/smart_aggregator.yaml`.

**Verificación:** Conectar a un YouTube Live y verificar que no crashea al recibir mensajes.

---

## TSH-004: Emotes de YouTube generaban respuestas sin sentido

**Síntoma:** Cuando el chat enviaba solo emotes (`:bird: :bird: :pogChamp:`), Kira respondía repitiendo el emote o generando texto sin relación.

**Causa raíz:** El Smart Aggregator procesaba mensajes que contenían solo aliases de emotes como texto semántico válido.

**Fix:** `smart_aggregator/message_filter.py` — Agregar regex para detectar y descartar mensajes con solo emotes. Limpiar aliases `:nombre:` cuando están mezclados con texto real.

**Verificación:** Enviar `:bird: :bird: :bird:` en un chat live. No debe llegar al aggregator.

---

## TSH-005: TTS chunk timeout en modo pesado (Qwen3-TTS)

**Síntoma:**
```
requests.exceptions.Timeout
```
en fragmentos largos de TTS pesado.

**Causa raíz:** Qwen3-TTS tarda más en generar audio para oraciones largas. El timeout default era insuficiente.

**Fix:** `config/settings.py` — `TTS_HEAVY_TIMEOUT` aumentado. Verificar valor actual en settings.

**Verificación:** Generar una respuesta larga de Kira (>50 palabras). Todos los chunks deben sintetizarse sin timeout.

---

## TSH-006: pkg_resources deprecation warning (pygame)

**Síntoma:**
```
UserWarning: pkg_resources is deprecated as an API.
```

**Causa raíz:** pygame 2.6.1 usa `pkg_resources` internamente, que está deprecado en setuptools >= 81.

**Estado:** **Warning cosmético, no es bug.** No afecta funcionalidad. Se puede ignorar o fijar `setuptools<81` si molesta.

---

## TSH-007: Ollama devuelve respuesta vacía

**Síntoma:** El LLM responde con string vacío o solo caracteres de control (`\x00`, `\ufeff`).

**Causa raíz:** Algunos modelos (especialmente los de razonamiento como `e2b`, `think`) pueden devolver `thinking` interno sin `content` visible.

**Fix:** `core/llm_engine.py` — Implementar retry loop (max 2 intentos) con `time.sleep(0.5)` entre intentos. Detectar modelos de razonamiento y remover `num_predict` limit.

**Verificación:** Si un modelo devuelve vacío, debe reintentar una vez antes de dar error.

---

## TSH-008: Modelos no se liberan de VRAM al cambiar

**Síntoma:** Al cambiar de modelo en Ollama, la VRAM no se libera y eventualmente hay OOM.

**Causa raíz:** Ollama mantiene el modelo en memoria por defecto (`keep_alive` default).

**Fix:** `core/llm_engine.py` — Llamar `self.ollama.generate(model=self.current_model, prompt='', keep_alive=0)` antes de cambiar al nuevo modelo. El nuevo modelo se carga con `keep_alive=-1` (permanente).

**Verificación:** Cambiar de modelo 3 veces seguidas. `nvidia-smi` debe mostrar liberación de VRAM entre cambios.

---

## TSH-009: OAuth YouTube — 429 Too Many Requests

**Síntoma:**
```
HTTP 429: Too Many Requests
```
al conectar a YouTube Live Chat.

**Causa raíz:** YouTube rate-limited las peticiones de `pytchat` cuando hay muchas conexiones o el `video_id` es de un stream muy activo.

**Fix:** `smart_aggregator/chat_source.py` — Implementar backoff exponencial con reintentos. Callbacks de error/conexión/desconexión para que la UI registre el fallo y recupere el estado del botón.

**Verificación:** Si YouTube devuelve 429, la UI debe mostrar aviso de reconexión y reintentar automáticamente.

---

## TSH-010: Gemma no soporta num_ctx

**Síntoma:**
```
Error Ollama: invalid option: num_ctx
```

**Causa raíz:** El modelo `gemma` de Google no acepta el parámetro `num_ctx` en las opciones de generación de Ollama.

**Fix:** `core/llm_engine.py` — Detectar "gemma" en el nombre del modelo y remover `num_ctx` de `opciones_llm` antes de llamar `ollama.chat()`.

**Verificación:** Seleccionar modelo gemma y enviar un mensaje. No debe dar error de opción inválida.

---

## TSH-011: PTT no funcionaba — transcripciones se descartaban al soltar la tecla

**Síntoma:** El streamer presionaba F8, hablaba, soltaba, y Kira no respondía. Los logs mostraban:
```
[PTT] MATCH press: Key.f8
[PTT] MATCH release: Key.f8
[LiveAudio]: parece que el match no sirve.
```
Pero la transcripción llegaba **después** de soltar F8 y se descartaba.

**Causa raíz:** PTT era solo un filtro booleano: `if ptt_enabled and not ptt_active → descartar`. LiveAudio tiene 1-2 segundos de delay entre el habla y la transcripción STT. Si el streamer soltaba F8 antes de que LiveAudio enviara la transcripción, llegaba con `ptt_active = False` → **se perdía**.

Además:
- Si el motor estaba hablando, la transcripción se descartaba (busy check)
- Si llegaban transcripciones parciales ("hola", "hola cómo", "hola cómo estás"), cada una era una consulta independiente al LLM
- YouTube chat y PTT competían sin prioridad — el primero que llegaba ganaba, el otro se perdía

**Fix (ADR-008):**
1. **Buffer PTT**: Mientras F8 está presionada, acumular TODAS las transcripciones en un buffer (máx 500 chars)
2. **Grace period de 2s**: Al soltar F8, seguir aceptando transcripciones 2 segundos más (delay STT)
3. **Flush watcher**: Hilo background que vigila el deadline del grace period y envía el buffer automáticamente
4. **Cola prioritaria**: PTT = prioridad 0, chat = prioridad 1. Máx 5 items, FIFO por prioridad
5. **Buffer de acumulación**: Overflow de cola y mensajes mientras motor ocupado → se guardan (máx 50 items, 2000 chars, TTL 2 min) y se compactan en 1 consulta cuando el motor queda libre

**Verificación:**
1. Presionar F8, hablar, soltar inmediatamente → Kira debe responder con la frase completa
2. Presionar F8, hablar, soltar, seguir hablando 1 segundo más → transcripciones del grace period deben incluirse
3. Con YouTube chat activo + PTT simultáneo → PTT debe procesarse primero, chat después
4. Kira hablando + PTT → voz del streamer debe encolarse y procesarse al terminar
