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

---

## ADR-009: Filtros de Calidad de Chat — Basura No Llega al Modelo

**Fecha:** 2026-05-12  
**Estado:** Activa

### Contexto

En streams con chat activo (ej: Abraham), el Smart Aggregator recibía cientos de mensajes por minuto, la mayoría sin valor semántico:

- `wwwwwwwwwwwwwwwwwwww` (100+ w's)
- `feeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee`
- `HOLA YT HOLA YT HOLA YT` (repetido 20+ veces)
- `╔╗ ╔╗╔═══╗╔╗` (ASCII art)
- `jsklsbfkfofii` (gibberish sin vocales)
- `hola me saludas` (3 palabras, sin contexto)

El modelo LLM recibía todo esto y generaba respuestas genéricas repetitivas como *"¡Este chat es un poco loco! ¡Alguien aquí debe tener una gran cantidad de energía positiva en su vida!"* — **usando una escopeta para matar moscas**.

### Decisión

Agregar 5 filtros de calidad a `MessageFilter` que operan **después** de los filtros básicos (longitud, emotes, links, menciones):

| Filtro | Qué detecta | Ejemplo descartado |
|--------|------------|-------------------|
| **Carácter repetitivo** | Un solo char >50% del mensaje | `wwwwwwwwww`, `feeeeeeeeee` |
| **Palabras repetidas** | Misma palabra >3 veces o ratio único <25% | `fe fe fe fe fe fe` |
| **Gibberish** | Ratio de vocales <10% | `jsklsbfkfofii`, `hsklsbfkfofii` |
| **ASCII art** | Box-drawing chars o líneas de `===`/`---` | `╔╗ ╔╗╔═══╗`, `==========` |
| **Quality score** | Penaliza mensajes cortos (≤4 palabras) o baja diversidad | `hola me saludas` → score 0.3 |

El aggregator ahora rechaza mensajes con `quality < min_quality_score` (default 0.3).

### Por qué

1. **El modelo no es un filtro**: Llamar a Ollama con basura consume VRAM, tiempo y genera respuestas irrelevantes
2. **Contexto importa**: Kira necesita contexto real del chat para reaccionar de forma genuina, no genérica
3. **Rendimiento**: Filtrar antes del LLM es O(n) barato vs inferencia LLM que cuesta segundos

### Tradeoffs aceptados

- **Falsos negativos**: Un mensaje legítimo con muchas letras repetidas (ej: "holaaaaa") podría descartarse si el ratio supera el threshold
- **Configuración necesaria**: Los thresholds (`repetitive_char_threshold: 0.50`, `min_vowel_ratio: 0.10`) pueden necesitar ajuste según el tipo de audiencia
- **No es perfecto**: Algunos spam sofisticado podría pasar (ej: mensajes variados pero sin sentido)

### Configuración

```yaml
filter:
  discard_repetitive_chars: true
  repetitive_char_threshold: 0.50
  discard_repeated_words: true
  repeated_word_max: 3
  min_unique_word_ratio: 0.25
  discard_gibberish: true
  min_vowel_ratio: 0.10
  discard_ascii_art: true
  short_word_threshold: 4
  min_quality_score: 0.3
```

---

## ADR-010: Intent Aggregator Portable — Intenciones Sí, Nombres Hardcodeados No

**Fecha:** 2026-05-12  
**Estado:** Activa

### Contexto

Después de filtrar basura del chat, un directo grande seguía produciendo cientos de mensajes válidos pero repetitivos: saludos, pedidos para jugar, solicitudes de amistad, preguntas sobre el juego, pedidos de videos/collabs, tradeos y copypasta.

La primera versión del agrupador de intenciones incluyó nombres de un canal específico como señales (`Abraham`, `Fede`, `Bros`, `Fernanfloo`, etc.). Eso funcionaba para ese stream, pero era **overfitting**: para otro streamer, esos nombres introducen sesgo y falsos positivos.

### Decisión

Separar **intención** de **entidad**:

- **Intención**: qué quiere el chat (`saludo`, `jugar/unirse`, `collab/video`, `sugerencia`, `trade`, etc.)
- **Entidad**: de quién o qué habla (`Fede`, `Bros`, `Coraline`, `garama`, etc.)

`IntentClassifier` usa patrones estructurales genéricos:

- `me saludas` → `greeting_request`
- `me puedo unir` → `join_request`
- `video con X` → `video_collab`, entidad `X`
- `conoces a X` → `video_collab`, entidad `X`
- `juega X` → `game_suggestion`, entidad `X`
- `tradeas por X` → `trade_request`, entidad `X`

Los nombres propios ya no son reglas globales. Si un nombre aparece muchas veces, se trata como entidad dinámica o tema frecuente, no como lógica hardcodeada.

### Por qué

1. **Portabilidad**: Kira debe servir para cualquier streamer, no solo para un canal específico
2. **Menos falsos positivos**: Un nombre propio aislado no debería disparar una intención
3. **Mejor contexto para LLM**: Kira recibe temas dominantes con entidades representativas, no mensajes crudos ni reglas sesgadas

### Tradeoffs aceptados

- **Menos detección específica**: Si el chat menciona solo un nombre sin estructura (`Fede`) queda como `other` hasta que sea tema frecuente
- **Extracción imperfecta**: Las entidades se extraen con regex simple, no NLP pesado
- **Reglas generales primero**: Diccionarios por canal pueden agregarse después, pero no como default global

### Ejemplo de salida

```text
Resumen del chat por intenciones dominantes:
- 32 mensajes: pedidos para jugar o unirse al servidor.
- 18 mensajes: pedidos de videos o colaboraciones. Temas/personas: los bros, fede.
- 12 mensajes: saludos, cumpleaños y pedidos de atención.
```

---

## ADR-011: StorageConfig Portable — El Disco de Cache lo Decide el Usuario

**Fecha:** 2026-05-13  
**Estado:** Activa

### Contexto

VoiceAI usa modelos y audio temporal pesado: Ollama, Hugging Face/Qwen, Torch, chunks TTS y archivos temporales de librerías. En una PC concreta, mover todo a `E:` puede resolver disco C: al 100%, pero hardcodear `E:\...` rompería portabilidad en otra máquina.

Además, aunque VoiceAI ya escribía muchos temporales en `E:\VoiceAI\temp`, las variables de Windows `TEMP`/`TMP` seguían apuntando a `C:\Users\...\AppData\Local\Temp`. Librerías como Torch, requests, soundfile o edge-tts pueden usar esas rutas sin pasar por nuestro `TEMP_DIR`.

### Decisión

Crear `config/storage.py` + `config/storage.yaml` como resolver central de almacenamiento:

```yaml
storage:
  cache_root: "auto"
  temp_root: "auto"
  ollama_models: "auto"
```

`auto` mantiene compatibilidad:

- `cache_root` → `modelos_f5` dentro del proyecto
- `temp_root` → `temp` dentro del proyecto
- `ollama_models` → respeta `OLLAMA_MODELS` existente o usa cache local

El usuario puede forzar otro disco:

```yaml
storage:
  cache_root: "D:/VoiceAI/cache"
  temp_root: "D:/VoiceAI/temp"
  ollama_models: "D:/OllamaStorage"
```

VoiceAI aplica por proceso:

- `TEMP`, `TMP`
- `HF_HOME`, `HUGGINGFACE_HUB_CACHE`, `TRANSFORMERS_CACHE`
- `TORCH_HOME`
- `OLLAMA_MODELS`

### Por qué

1. **Portabilidad**: No asumir `E:` ni ninguna letra de disco
2. **Menos presión sobre C:** mover temporales y caches de librerías fuera del perfil de usuario
3. **Control explícito**: el usuario decide disco rápido/grande según su PC
4. **Orden correcto**: las variables se aplican antes de importar modelos pesados

### Tradeoffs aceptados

- Si Ollama ya estaba corriendo antes de VoiceAI, puede seguir usando su ruta vieja hasta reiniciarlo
- Mover caches existentes entre discos sigue siendo responsabilidad del usuario o de una futura herramienta de migración
- El pagefile de Windows puede seguir usando C: si el sistema operativo está configurado así

---

## ADR-012: Smart Aggregator Guarda Contexto Compacto, No Chat Crudo

**Fecha:** 2026-05-13  
**Estado:** Activa

### Contexto

El Smart Aggregator acumuló 239.904 filas en `data/smart_aggregator/sessions.db` y un `chat_log.jsonl` de 51.681.730 bytes. La mayoría eran mensajes raw del chat, incluyendo basura ya rechazada por filtros. Kira, sin embargo, solo necesita el contexto compacto que se le entrega para reaccionar.

### Decisión

Producción persiste **snapshots compactos** en `context_snapshots`:

- resumen/contexto privado enviado a Kira,
- cantidad de mensajes considerados,
- vibe/metadata relevante,
- sesión y timestamp.

El guardado de chat raw queda **prohibido por código**. La configuración ya no ofrece `persist_raw_messages` ni `persist_rejected_messages`:

```yaml
history:
  db_path: "data/smart_aggregator/sessions.db"
  retention_hours: 168
```

Si hace falta debugging, debe hacerse con logs temporales explícitos fuera de `SessionHistory`; la memoria de Kira no persiste comentarios crudos.

### Por qué

1. **Memoria útil > ruido:** guardar 200k mensajes para usar 12/20 recientes es desperdicio.
2. **Privacidad:** chat raw puede contener datos personales de usuarios.
3. **Rendimiento:** SQLite y backups no deberían crecer por basura efímera.
4. **Arquitectura correcta:** memoria operativa corta vive en RAM; memoria persistente guarda decisiones/contexto resumido.

### Limpieza operacional

Se agregó:

```powershell
E:\Miniconda\envs\flux_env\python.exe scripts/cleanup_smart_aggregator_db.py --execute
```

El script elimina la tabla legacy `messages` y `chat_log.jsonl`, preservando `context_snapshots`.

### Tradeoffs aceptados

- Se pierde auditoría completa del chat porque raw logging queda prohibido en `SessionHistory`.
- Algunos diagnósticos de spam requerirán reproducir con logs temporales explícitos fuera de la memoria de Kira.
- Los historiales viejos raw no se migran automáticamente a summaries porque compactar 200k mensajes post-facto podría inventar contexto pobre.

---

## ADR-013: Kira Co-host Agenda Mode sobre Autonomía Total

**Fecha:** 2026-05-13  
**Estado:** Propuesta aceptada para diseño; implementación pendiente

### Contexto

Un modo full autónomo donde Kira dirige el stream indefinidamente puede saturar Ollama/GPU, repetir ideas, filtrar texto interno del prompt o inventar dirección sin control humano. El objetivo de producto es que Kira pueda cubrir momentos donde el streamer se ausenta o necesita soporte, sin convertirla en un sistema impredecible.

### Decisión

Diseñar Kira como **co-host semi-autónoma con agenda aprobada**:

- el streamer prepara o aprueba temas cortos,
- Kira desarrolla un tema por turnos breves,
- PTT del streamer tiene prioridad máxima,
- chat filtrado/compactado puede influir como señal secundaria,
- las sugerencias de futuros temas son borradores y requieren aprobación,
- el modo tiene stop suave y stop de emergencia.

El diseño completo vive en [`docs/KIRA_COHOST_AGENDA_MODE.md`](./KIRA_COHOST_AGENDA_MODE.md).

### Por qué

1. **Control humano:** la dirección editorial sigue siendo del streamer.
2. **Determinismo:** una state machine evita loops caóticos.
3. **Rendimiento:** no hay generación infinita ni precola agresiva.
4. **Seguridad:** prompts, sanitizer y estados de salida reducen leaks/alucinaciones.
5. **Producto atractivo:** Kira se siente como co-host real, no como bot aleatorio.

### Tradeoffs aceptados

- Requiere UI de agenda y más tests antes de implementación.
- Kira será menos “libre”, pero mucho más confiable en vivo.
- El MVP no incluirá vector DB ni escritura automática al chat.
