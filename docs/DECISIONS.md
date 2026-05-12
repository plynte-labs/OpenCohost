# Architecture Decision Records — VoiceAI

Decisiones de arquitectura y diseño tomadas durante el desarrollo. Cada registro explica el **por qué**, no solo el **qué**.

---

## ADR-001: Local-First — Privacidad y Seguridad sobre Cloud

**Fecha:** 2026-05  
**Estado:** Activa  
**Revisable cuando:** Usuarios pidan modelos cloud o se resuelva gestión segura de API keys

### Contexto
VoiceAI maneja credenciales de streaming (YouTube OAuth), audio de voz del streamer, y datos de chat en tiempo real. Todo esto es información sensible de un creador de contenido.

### Decisión
Todo el procesamiento es **local-first**:
- LLM: Ollama local (no API cloud)
- TTS pesado: Qwen3-TTS local (no ElevenLabs, no Google Cloud)
- TTS ligero: edge-tts (solo como fallback, no requiere credenciales)
- OAuth tokens: archivo local ignorado por git
- Modelos cacheados: `modelos_f5/hub` para operación offline

### Por qué
1. **Seguridad del canal**: Si alguien roba credenciales OAuth de YouTube, puede tomar control del canal. Perder la cookie de streamer es equivalente a perder la cuenta. Local-first minimiza la superficie de ataque.
2. **Privacidad**: La voz del streamer es su identidad. Enviarla a servicios cloud TTS crea huella digital permanente fuera de su control.
3. **Costos cero**: Sin API keys = sin facturas sorpresa. El streamer no paga por inferencia LLM ni por TTS.
4. **Offline-resilient**: En LATAM la conexión de internet es inestable. Si se cae la red, el TTS pesado local sigue funcionando.

### Tradeoffs aceptados
- **Mayor uso de RAM/VRAM**: Modelos locales consumen recursos del equipo del streamer
- **Setup inicial más complejo**: Descargar modelos vs solo poner una API key
- **Menor calidad TTS ligero**: edge-tts es inferior a ElevenLabs, pero no requiere red ni credenciales

### Futuro
Si usuarios piden modelos cloud, se agregará como **opción explícita** (no default). Las API keys se guardarían en Windows Credential Manager (keyring), nunca en texto plano.

---

## ADR-002: PTT como Solución Principal a Alucinaciones de Whisper

**Fecha:** 2026-05  
**Estado:** Activa

### Contexto
Whisper tiene un bug clásico: cuando escucha ruido de fondo o su propio audio de salida (feedback loop), transcribe patrones repetitivos como "gracias gracias gracias gracias...". Esto se envía al LLM, que lo interpreta como input real y genera respuestas basadas en ruido. El resultado: **Kira se pone a hablar sola**, creando un loop infinito de alucinaciones.

### Decisión
Push-to-Talk (PTT) como compuerta principal:
- Solo se envía audio al pipeline cuando el streamer **activamente presiona** una tecla
- Silero VAD como filtro secundario (detecta voz humana vs ruido)
- Half-duplex: si Kira está hablando, ignora cualquier input nuevo
- Filtro anti-loop en `voice_control.py`: detecta y deduplica palabras repetidas consecutivas (>3 veces)

### Por qué
1. **Solución determinista**: PTT es binario — o presionaste o no. No hay ambigüedad como con VAD-only.
2. **Cero falsos positivos por feedback**: Si no presionás, no se envía nada. Punto.
3. **Compatible con gaming**: El streamer puede mapear PTT a Mouse4/F10 mientras juega sin interrumpir.

### Tradeoffs aceptados
- **Menos "conversación libre"**: El streamer tiene que activamente presionar para hablar. No hay modo "siempre escuchando" confiable sin falsos positivos.
- **Dependencia de hardware**: Necesita una tecla disponible que no colisione con el juego.

### Capas de defensa (en orden)
1. **PTT gate** — solo captura cuando se presiona
2. **Silero VAD** — descarta si no hay voz humana
3. **Anti-loop filter** — sanitiza transcripción antes de enviar al LLM
4. **Half-duplex** — bloquea input mientras Kira habla

---

## ADR-003: Pausar Panel de Administración de YouTube (Stream Admin)

**Fecha:** 2026-05  
**Estado:** Pausado — MVP implementado, escritura deshabilitada por defecto

### Contexto
RF4 implementó integración con YouTube Data API v3 para administrar streams: cambiar título, categoría, moderar chat, leer analíticas. Esto requiere OAuth 2.0 con scopes de escritura (`youtube.force-ssl`).

### Decisión
El panel de Stream Admin queda **pausado en modo lectura**. La UI existe, los endpoints están implementados, pero las acciones de escritura están deshabilitadas por defecto y requieren aprobación explícita del streamer.

### Por qué
1. **Riesgo catastrófico**: Si alguien roba los tokens OAuth con scope de escritura, puede:
   - Cambiar el título del stream a contenido ofensivo
   - Borrar videos del canal
   - Banear usuarios legítimos del chat
   - Enviar mensajes al chat como el streamer
2. **Complejidad de OAuth**: El flujo de OAuth 2.0 para YouTube requiere:
   - Crear proyecto en Google Cloud Console
   - Configurar pantalla de consentimiento (revisión de Google)
   - Generar Client ID + Secret
   - Manejar refresh tokens con expiración
   - Rotación de credenciales
3. **Local-first coherente**: Almacenar tokens de escritura localmente (aunque sea en archivo ignorado por git) es un riesgo aceptable solo para lectura. Para escritura, se necesitaría Windows Credential Manager como mínimo.

### Tradeoffs aceptados
- **Funcionalidad limitada**: El streamer no puede automatizar cambios de título desde Kira
- **MVP incompleto**: La UI está pero los botones de acción están bloqueados
- **Valor percibido menor**: El panel parece "a medio hacer" para quien no conoce el contexto de seguridad

### Futuro
Si se habilita escritura:
1. Migrar tokens a `keyring` (Windows Credential Manager)
2. Implementar rotación automática de refresh tokens
3. Agregar confirmación de 2 pasos para acciones de alto riesgo (ban, cambio de título)
4. Logging de auditoría de todas las acciones ejecutadas

---

## ADR-004: TTS Ligero (edge-tts) Sin Modo Offline

**Fecha:** 2026-05  
**Estado:** Activa

### Contexto
VoiceAI tiene dos motores TTS:
- **Pesado**: Qwen3-TTS local — funciona offline, clona voz del streamer
- **Ligero**: edge-tts (Microsoft) — requiere internet, voz genérica

### Decisión
El modo ligero **no tiene soporte offline**. Si no hay internet, el streamer debe cambiar manualmente al modo pesado.

### Por qué
1. **edge-tts es inherentemente cloud**: Es un servicio de Microsoft Azure. No hay modelo local que descargar.
2. **Tradeoff intencional**: El modo ligero existe para equipos sin GPU dedicada. Si tenés GPU, usá el modo pesado offline. Si no tenés GPU, necesitás internet de todos modos para edge-tts.
3. **UX clara**: El log muestra explícitamente: "Edge-TTS requiere internet. Si estás offline usa Pesado (Qwen3-TTS)."

### Tradeoffs aceptados
- **Sin fallback automático**: Si se cae internet durante modo ligero, el TTS falla hasta que el usuario cambie manualmente
- **Confusión potencial**: Usuarios nuevos no entienden por qué un modo funciona offline y el otro no

### Futuro
Podría agregarse un tercer motor TTS local ligero (ej: Piper TTS) que funcione offline sin GPU, pero aumenta la complejidad de instalación y uso de disco.

---

## ADR-005: Filtro de Emotes de YouTube Chat

**Fecha:** 2026-05  
**Estado:** Activa

### Contexto
El chat de YouTube Live tiene emotes personalizados con formato `:nombre:` (ej: `:bird:`, `:pogChamp:`). Cuando un mensaje contiene solo emotes o spam de emotes, el Smart Aggregator lo procesaba y el LLM lo repetía o generaba respuestas sin sentido.

### Decisión
`MessageFilter` en `smart_aggregator/message_filter.py`:
- Descarta mensajes que contienen **solo** emotes o spam repetido
- Limpia aliases de emotes (`:bird:` → string vacío) cuando están mezclados con texto real
- Regex reforzada para detectar menciones con guiones/puntos
- Rate-limit por usuario configurable (`Max/u` en UI)
- Deduplicación de mensajes repetidos por usuario

### Por qué
1. **Ruido al LLM**: Emotes no son información semántica. Enviarlos al LLM genera alucinaciones o respuestas genéricas.
2. **Spam protection**: Un usuario enviando el mismo emote 20 veces no aporta contexto.
3. **Costo computacional**: Cada mensaje filtrado es una inferencia LLM menos necesaria.

### Tradeoffs aceptados
- **Falsos negativos**: Un mensaje legítimo que solo contiene un emote (ej: "🎉" como celebración genuina) se descarta
- **Mantenimiento de regex**: Los emotes nuevos o formatos cambiantes requieren actualizar patrones

---

## ADR-006: Motor IA — Arranque Diferido (Race Condition Fix)

**Fecha:** 2026-05-11  
**Estado:** Activa

### Contexto
El hilo `MotorVocalIA` se inicializaba en `__init__` de la ventana Tkinter y llamaba `start()` inmediatamente. Cuando Ollama ya estaba corriendo, `_check_ollama_service()` terminaba rápido y llamaba `ui_callback("ready")` → `self.after(0, ...)`. Pero `mainloop()` aún no había arrancado.

### Decisión
`self.after(100, self._start_motor)` en lugar de `self.motor_ia.start()` directo en `__init__`.

### Por qué
Tkinter's `after()` requiere que el main loop esté activo. Sin el defer:
```
RuntimeError: main thread is not in main loop
```

### Tradeoffs
- **100ms de delay** en la inicialización del motor (imperceptible para el usuario)
- Regla general: cualquier hilo que invoque callbacks UI vía `self.after()` debe iniciarse después de que `mainloop()` esté corriendo

---

## ADR-007: Single LLM — No Instanciar Segundo Modelo

**Fecha:** 2026-05  
**Estado:** Activa

### Contexto
El Smart Aggregator (RF3) necesita analizar sentimiento del chat (Vibe Thermometer). La tentación era crear una instancia separada de Ollama para esto.

### Decisión
El aggregator **reutiliza** el `MotorVocalIA` existente mediante `llm_interface` inyectada. No instancia un segundo modelo.

### Por qué
1. **VRAM limitada**: En una RTX 3060 (12GB), cargar dos modelos LLM simultáneamente causa OOM.
2. **Coherencia de contexto**: Un solo modelo = un solo historial conversacional. Dos modelos = respuestas inconsistentes.
3. **Costo**: Un solo proceso Ollama consume menos RAM y CPU.

### Tradeoffs
- **Acoplamiento**: El aggregator depende de que el motor esté disponible
- **Latencia**: Si el motor está procesando una respuesta, el vibe analysis espera

---

## ADR-008: Cola Prioritaria con Buffer PTT y Acumulación Inteligente

**Fecha:** 2026-05-12  
**Estado:** Activa

### Contexto

PTT (Push-to-Talk) era solo un "filtro" sobre transcripciones del WebSocket de LiveAudio: si el streamer presionaba F8, se aceptaban transcripciones; si soltaba, se descartaban. Esto tenía múltiples problemas:

1. **Sin buffering**: Si LiveAudio tenía 1-2 segundos de delay entre el habla y la transcripción, al soltar F8 la transcripción llegaba cuando `ptt_active = False` → **se descartaba**
2. **Sin prioridad**: YouTube chat y PTT competían por la misma cola. Si llegaban juntos, el primero que entraba ganaba y el otro se descartaba silenciosamente
3. **Sin acumulación**: Si el motor estaba ocupado procesando, cualquier mensaje nuevo (PTT o chat) se perdía para siempre
4. **Frases cortadas**: LiveAudio envía transcripciones parciales. Si el streamer decía "hola cómo estás hoy", podían llegar 3 mensajes separados: "hola", "hola cómo estás", "hola cómo estás hoy" — cada uno como consulta independiente al LLM

El resultado: **Kira respondía a fragmentos de frases, perdía input del streamer, y no había forma de priorizar la voz del streamer sobre el chat**.

### Decisión

Implementar un sistema de **cola prioritaria secuencial con buffer de acumulación**:

#### 1. Buffer PTT con Grace Period

```
Presiona F8 → buffer se limpia, empieza a acumular
  [LiveAudio]: "hola" → buffer: "hola"
  [LiveAudio]: "hola cómo estás" → buffer: "hola cómo estás"
Suelta F8 → grace period de 2 segundos
  [LiveAudio]: "hola cómo estás hoy" → buffer: "hola cómo estás hoy"
Grace period expira → flush automático → envía como UNA consulta
```

- **Grace period de 2s**: LiveAudio tiene delay STT. Sin gracia, las transcripciones llegan después de soltar la tecla y se pierden
- **Flush watcher thread**: Hilo background que vigila el deadline del grace period. Sin esto, si no llegan transcripciones durante la gracia, el buffer nunca se envía
- **Límite 500 chars**: Evita que un streamer hablando 10 minutos acumule un texto gigante

#### 2. Cola Prioritaria (máx 5 items)

```
Prioridad 0 (alta): PTT — el streamer hablando
Prioridad 1 (normal): YouTube chat — mensajes del chat
```

- FIFO por prioridad: PTT siempre antes que chat
- Si cola llena → item más viejo va a buffer de acumulación (no se pierde)
- **Nunca dos peticiones a Ollama a la vez**: el motor procesa secuencialmente

#### 3. Buffer de Acumulación (máx 50 items, 2000 chars, TTL 2 min)

Guarda todo lo que no pudo procesarse inmediatamente:
- Mensajes descartados de la cola prioritaria (overflow)
- Transcripciones PTT cuando el motor está hablando
- Chat de YouTube cuando el motor está procesando

Cuando el motor queda libre:
1. Procesa siguiente item de cola prioritaria
2. Si cola vacía → compacta acumulación en 1 consulta:
   ```
   "Mientras procesabas, llegaron estos mensajes del chat:
   User1: 'jajaja' | User2: 'qué hizo?' | User3: 'no entendí'"
   ```
3. Envía como UNA sola consulta → limpia buffer

### Por qué

1. **PTT no interrumpe**: Si Kira está hablando, la voz del streamer espera su turno. No cortamos TTS a mitad de frase
2. **No perdemos contexto**: Todo lo que llega mientras el motor está ocupado se acumula y se procesa después
3. **Prioridad justa**: La voz del streamer (interrupción deliberada) tiene prioridad sobre el chat (background noise)
4. **Rendimiento controlado**: Límites estrictos (5 items cola, 50 items acumulación, 2000 chars, 2 min TTL) evitan fugas de memoria
5. **Una consulta a la vez**: El motor nunca procesa dos cosas simultáneamente — evita OOM y respuestas inconsistentes

### Tradeoffs aceptados

- **Latencia PTT**: Si Kira está hablando, el streamer espera a que termine antes de que su voz se procese (no hay interrupción)
- **Chat "viejo"**: Mensajes de chat con >2 minutos se descartan automáticamente al compactar (pueden ser irrelevantes)
- **Complejidad**: 3 estructuras de datos (buffer PTT, cola prioritaria, acumulación) vs 1 cola simple anterior
- **Hilo extra**: Flush watcher corre cada 500ms — overhead mínimo pero existe

### Arquitectura del flujo

```
┌──────────────────────────────────────────────────────┐
│  PTT (F8) → Buffer (500 chars) → Grace 2s → Flush   │
│                                                      │
│  YouTube Chat → Cola Prioritaria (5 items, prio 1)   │
│  PTT Flush    → Cola Prioritaria (5 items, prio 0)   │
│                                                      │
│  Overflow cola → Buffer Acumulación (50/2000/120s)   │
│                                                      │
│  Motor libre → Cola → Acumulación compactada → Limpio│
└──────────────────────────────────────────────────────┘
```

### Futuro

- Si se quiere interrupción real (PTT corta TTS de Kira), agregar comando `stop_speaking` al motor
- Si el chat es muy activo, compactación más agresiva: agrupar por tema en vez de cronológico
- Considerar un "modo streamer" donde el chat se pausa completamente mientras PTT está activo
