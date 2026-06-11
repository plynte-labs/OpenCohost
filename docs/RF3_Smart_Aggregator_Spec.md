# RF3 - Smart Aggregator: Especificación para Agente IA

## ⚠️ Entorno Obligatorio

**Python:** activate your project Python environment; use `python` from the activated shell.

**PROHIBIDO:**
- ❌ `pip install` / `pip uninstall` / `conda install`
- ❌ Modificar, actualizar o eliminar paquetes del entorno de proyecto
- ❌ Usar `python` sin activar el entorno correcto primero

Si necesitas una dependencia nueva, **consulta primero**.

---

## Contexto del Sistema

El proyecto tiene **dos aplicaciones**:

1. **`VoiceAI`** — UI (`ui/app.py`), motor IA, TTS, LLM
2. **`LiveAudio`** — App separada que maneja:
   - Audio del micrófono
   - Transcripción Whisper
   - Detección Silero VAD
   - Envío de transcripciones via WebSocket

**Smart Aggregator (RF3)** consume chat de **YouTube Live**, NO audio de LiveAudio. No interactúa con Silero ni Whisper.

---

## Restricciones Arquitectónicas (CRÍTICAS)

1. **NO TOCAR `ui/app.py` NI `motor_ia.py`** — Si necesitas коммуникацию con el core, usacolas de mensajes o eventos.
2. **Módulo independiente en `smart_aggregator/`** — Todo el código nuevo va ahí.
3. **Un solo modelo LLM en ejecución** — No crear instancias adicionales de Ollama/LLM. Reutilizar el motor existente si ya está corriendo.
4. **Configuración via `config/smart_aggregator.yaml`** — Sin hardcoded.
5. **NO crear threadsglobals ni estado global** — Todo debe ser instanciable.

---

## Arquitectura Modular

```
smart_aggregator/
├── __init__.py
├── aggregator.py          # Orquestador principal
├── chat_source.py        # Fuente de chat (YouTube)
├── message_filter.py     # Filtros RF3.1
├── vibe_thermometer.py   # RF3.2 (un solo modelo LLM compartido)
├── activity_trigger.py   # RF3.3
├── session_history.py    # RF3.4 (persistencia híbrida)
└── config.yaml           # Configuración del módulo
```

**Interfaz con el core:** El `aggregator.py` recibe callbacks configurables para:
- `on_filtered_message(message)` — cuando un mensaje pasa los filtros
- `on_vibe_update(vibe_data)` — cuando la temperatura emocional cambia
- `on_activity_trigger(threshold_breached, msg_rate)` — cuando se supera el umbral
- `on_aggregated_context(context)` — cuando se genera contexto consolidado para Kira

---

## RF3.1 — Filtro de Longitud y Calidad

### Qué HACER:
- Crear clase `MessageFilter` en `message_filter.py`
- Cargar umbrales desde `config.yaml` (`min_words`, `min_char_length`, etc.)
- Descartar: mensajes cortos, emojis puros, enlaces, menciones (@usuario)
- Retornar `None` si el mensaje se descarta, o el mensaje limpio si pasa
- **Proveer whitelist de usuarios VIP/Mod** que pueden saltar filtros (configurable)

### Qué NO HACER:
- NO hardcodear umbrales ("5 palabras" no existe en el código, sale de config)
- NO modificar `ui/app.py` para añadir lógica de filtro
- NO crear su propio modelo LLM

### Interfaz:
```python
class MessageFilter:
    def __init__(self, config: dict)
    def filter(self, message: dict) -> Optional[dict]:  # dict = {"user": str, "text": str, "timestamp": float}
        # Retorna mensaje limpio o None si se descarta
```

### Configuración (`config.yaml`):
```yaml
filter:
  min_words: 3
  min_char_length: 10
  discard_emojis_only: true
  discard_links: true
  discard_mentions: true
  whitelist:
    enabled: true
    users: []  # usuarios que saltan filtro
```

---

## RF3.2 — Vibe Thermometer (una sola inferencia LLM)

### Qué HACER:
- Crear clase `VibeThermometer` en `vibe_thermometer.py`
- Usar ventana de tiempo configurable (default 120s)
- Acumular mensajes en buffer con timestamps
- Al finalizar ventana, hacer UNA inferencia LLM con todos los mensajes acumulados
- Analizar: excitement, sadness, anger, joy, confusion, neutral
- Retornar distribución de emociones + temperatura global (0-100)
- **Reutilizar conexión LLM existente** del core si ya está activa

### Qué NO HACER:
- NO crear nueva instancia de Ollama/LLM
- NO hacer inferencia por mensaje (lento y costoso)
- NO usar librería externa de sentiment analysis (solo LLM)

### Interfaz:
```python
class VibeThermometer:
    def __init__(self, config: dict, llm_interface)  # llm_interface = callable
    def add_message(self, message: dict)
    def compute_vibe(self) -> dict  # {"emotions": {...}, "temperature": float, "window_duration": int}
    def reset(self)
```

### Configuración:
```yaml
vibe:
  window_seconds: 120
  emotions:
    - excitement
    - sadness
    - anger
    - joy
    - confusion
    - neutral
  llm_prompt_template: "Analiza el sentimiento de estos mensajes de chat en los últimos {window}s. Mensajes: {messages}. Retorna JSON con emotions (0-1 cada uno) y temperature (0-100)."
```

---

## RF3.3 — Trigger por Actividad

### Qué HACER:
- Crear clase `ActivityTrigger` en `activity_trigger.py`
- Medir mensajes por segundo en ventana deslizante (configurable, default 5s)
- Callback cuando se supera threshold configurable
- Dos acciones posibles (configurables):
  1. `auto_reply`: Kira responde con mensaje predefinido
  2. `behavior_change`: Modifica parámetro de comportamiento (ej. más excitable)

### Qué NO HACER:
- NO hacer que Kira responda automáticamente sin configuración explícita
- NO usar threadsglobales para el conteo

### Interfaz:
```python
class ActivityTrigger:
    def __init__(self, config: dict, callbacks: dict)
    def on_message(self, message: dict)
    def get_current_rate(self) -> float
    def reset(self)
```

### Configuración:
```yaml
activity:
  window_seconds: 5
  threshold_per_second: 10.0
  actions:
    auto_reply:
      enabled: false
      message: "¡El chat está que explota! 🔥"
    behavior_change:
      enabled: false
      parameter: "excitement_multiplier"
      value: 1.5
```

---

## RF3.4 — Historial de Contexto (Persistencia Compacta por Sesiones)

### Qué HACER:
- Crear clase `SessionHistory` en `session_history.py`
- SQLite para persistencia estructurada por sesión
- Una "sesión" = desde que se conecta YouTube hasta que se desconecta
- Guardar solo **snapshots compactos**: el contexto resumido que Kira realmente recibió para hablar
- Prohibir persistencia de chat raw por código, incluso para flags legacy de configuración

### Qué NO HACER:
- NO usar la misma base de datos que el core (si existe)
- NO guardar todo indefinidamente — implementar retención configurable
- NO persistir chat crudo; 12/20 mensajes recientes se resuelven en memoria y se compactan antes de persistir

### Interfaz:
```python
class SessionHistory:
    def __init__(self, db_path: str, jsonl_path: str, retention_hours: int)
    def start_session(self, platform: str, channel: str) -> int  # retorna session_id
    def end_session(self, session_id: int)
    def add_context_snapshot(self, session_id: int, summary: str, message_count: int, vibe: float | None, metadata: dict)
    def get_recent_context_snapshots(self, session_id: int, max_items: int) -> list
    def get_session_context(self, session_id: int, max_messages: int) -> list  # legacy shim: siempre []
    def cleanup_old_sessions(self)  # llamado periódicamente
```

### Schema SQLite:
```sql
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY,
    platform TEXT,
    channel TEXT,
    start_time REAL,
    end_time REAL
);

CREATE TABLE context_snapshots (
    id INTEGER PRIMARY KEY,
    session_id INTEGER,
    summary TEXT,
    timestamp REAL,
    message_count INTEGER,
    vibe_temperature REAL,
    metadata_json TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
```

La tabla legacy `messages` puede existir en bases viejas, pero el código nuevo no la crea ni la llena. La memoria útil de producción vive en `context_snapshots`.

### Configuración:
```yaml
history:
  db_path: "data/smart_aggregator/sessions.db"
  retention_hours: 168  # 7 días
```

### Limpieza de DB existente:

```powershell
python scripts/cleanup_smart_aggregator_db.py --dry-run
python scripts/cleanup_smart_aggregator_db.py --execute
```

El script borra datos legacy raw de `messages` y el `chat_log.jsonl`; preserva `context_snapshots`.

---

## RF3.5 — Chat Source (YouTube)

### Qué HACER:
- Crear clase `YouTubeChatSource` en `chat_source.py`
- Usar `pytchat` o `chatdownload` para ingestar chat de YouTube Live
- Proveer interfaz de callbacks (`on_message(message)`)
- Handle reconnects automáticos

### Qué NO HACER:
- NO implementar scraping manual de YouTube (viola ToS)
- NO hardcodear video IDs

### Interfaz:
```python
class YouTubeChatSource:
    def __init__(self, config: dict, callbacks: dict)
    def connect(self, video_id: str)  # video_id de la URL de YouTube
    def disconnect(self)
    def is_connected(self) -> bool
```

### Configuración:
```yaml
source:
  platform: youtube  # futuro: youtube, twitch
  reconnect_delay_seconds: 5
  max_retries: 3
```

---

## Integración con Core (Contrato de Eventos)

El `smart_aggregator` se comunica via callbacks configurados por `ui/app.py`. **NO se modifica `ui/app.py` para añadir lógica de RF3**, solo se instancia y conecta el módulo.

```python
# En ui/app.py (solo instanciación, SIN lógica de negocio):
from smart_aggregator import Aggregator

self.smart_agg = Aggregator(config_path="config/smart_aggregator.yaml")
self.smart_agg.on_filtered_message = self._on_kira_input  # conecta al pipeline existente
self.smart_agg.on_vibe_update = self._update_vibe_display
self.smart_agg.connect(video_id="...")
```

---

## Dependencias Permitidas

- `pytchat` o `chatdownload` (YouTube)
- `sqlite3` (stdlib)
- `pyyaml` (ya existe en proyecto)
- `requests` (ya existe)

## Dependencias PROHIBIDAS

- `transformers`, `torch`, `tensorflow` (no crear modelos nuevos)
- `nltk`, `textblob`, `vaderSentiment` (sin sentiment analysis externo)

---

## Criterios de Aceptación

1. `smart_aggregator/` es importable sin errores en entorno existente
2. Todos los valores configurables vienen de `config.yaml`
3. `test_local.py` puede ejecutar cada clase independientemente con datos mock
4. No se modifica ningún archivo fuera de `smart_aggregator/` y `config/`
5. El módulo corre sin UI (headless) para testing
6. Whisper, TTS, LLM del core siguen funcionando identical

---

## Notas de Implementación — Respuestas del AIWorker

Las siguientes decisiones de diseño y arquitectura fueron tomadas por el agente constructor durante la implementación del RF3, con base en el análisis del entorno existente y las respuestas del usuario.

**[Respuesta AIWorker]: Decisión 1 — Dependencia `pytchat`**
El usuario confirmó preferir `pytchat` sobre `chatdownload`. Sin embargo, `pytchat` no está instalado en `flux_env` y las instrucciones del agente prohíben ejecutar `pip install`. Por ello, `chat_source.py` se implementó con un `try/except` alrededor del import de `pytchat`. Si no está disponible, la clase `YouTubeChatSource` opera en modo "mock" (simulación) para que `test_local.py` pueda ejecutarse sin errores, y lanza una excepción clara si se intenta `connect()` sin la librería. El usuario debe instalar `pytchat` manualmente cuando lo requiera.

**[Respuesta AIWorker]: Decisión 2 — Detección de emojis sin librería externa**
La librería `emoji` no está disponible en `flux_env`. En lugar de instalarla (prohibido), `message_filter.py` implementa una función `_has_only_emojis()` basada en rangos Unicode de emoticonos y símbolos gráficos. Esto evita una dependencia adicional mientras cumple con el requisito RF3.1 de descartar mensajes que solo contienen emojis.

**[Respuesta AIWorker]: Decisión 3 — Ubicación del archivo de configuración**
Se eligió `config/smart_aggregator.yaml` (configuración centralizada) conforme a la preferencia del usuario y la restricción arquitectónica #3 del spec. `aggregator.py` resuelve rutas relativas desde la raíz del proyecto (`BASE_DIR`), garantizando que funcione independientemente del directorio de ejecución.

**[Respuesta AIWorker]: Decisión 4 — Interfaz LLM compartido en VibeThermometer**
Para cumplir estrictamente la restricción "un solo modelo LLM en ejecución", `VibeThermometer` acepta `llm_interface: callable` y ya no importa ni llama `ollama` directamente. Si el core quiere análisis real de vibe, debe inyectar su interfaz LLM compartida al crear `Aggregator` o mediante `set_llm_interface()`. Si no hay interfaz LLM, el thermometer devuelve un fallback neutral documentado y no carga modelos por su cuenta.

**[Respuesta AIWorker]: Decisión 5 — Manejo de LLM ocupado en segundo plano**
El `Aggregator` mantiene `is_busy_callback`: si retorna `True` (por ejemplo, `MotorVocalIA` en `processing` o `speaking`), el `VibeThermometer` salta la inferencia y retorna una temperatura neutral configurable con distribución mayoritariamente `neutral`. Cuando el sistema está idle, solo ejecuta inferencia si existe `llm_interface` inyectada.

**[Respuesta AIWorker]: Decisión 6 — Persistencia híbrida SQLite + JSONL**
`SessionHistory` mantiene SQLite como fuente de verdad para consultas estructuradas (por sesión, por usuario, por rango de tiempo) y JSONL como audit trail. El cleanup borra sesiones antiguas de SQLite y reescribe JSONL excluyendo los mismos `session_id`, para que ambos medios conserven la misma política de retención sin penalizar inserciones frecuentes.

**[Respuesta AIWorker]: Decisión 7 — Orden de implementación real**
Se siguió el orden sugerido por el spec: RF3.4 → RF3.1 → RF3.5 → RF3.2 → RF3.3 → Orquestador → Config → Tests. Esto permitió validar cada componente de abajo hacia arriba, asegurando que `Aggregator` tuviera todas sus dependencias listas antes de ser cableado.

**Correcciones de revisión — 2026-05-04**
Se corrigió RF3 para alinearlo con las restricciones críticas: `VibeThermometer` ya no usa Ollama directamente y depende de `llm_interface`; `ActivityTrigger` calcula mensajes/segundo con ventana configurable y timestamps del mensaje; `YouTubeChatSource.connect()` evita deadlock al reconectar y ya no recomienda ejecutar `pip install`; `Aggregator` acepta `llm_interface`, permite sesión headless para pruebas y solo alimenta el vibe con mensajes filtrados; `SessionHistory.cleanup_old_sessions()` elimina del JSONL por `session_id`; `test_local.py` valida emojis reales, acciones de trigger, retención SQLite+JSONL y orquestación con config temporal. Verificado con `python -c "from smart_aggregator import Aggregator; print('Import OK')"` y `python smart_aggregator/test_local.py`.

**Integración UI autorizada — 2026-05-04**
El usuario autorizó la opción 2 para el paso 3: integrar RF3 desde `ui/app_shell.py` mediante un adapter LLM silencioso, sin modificar `core/llm_engine.py`. La UI instancia `Aggregator`, inyecta `llm_interface` usando el modelo activo de `MotorVocalIA`, registra `busy_callback` para no competir cuando Kira está procesando/hablando, agrega controles para pegar URL o `video_id` de YouTube Live y conecta callbacks de log, vibe, actividad y contexto agregado. Kira no recibe cada mensaje individual; solo se envía contexto agregado cuando `ActivityTrigger` detecta pico, evitando spam al LLM/TTS. Verificado con `python -m py_compile ui\app_shell.py` y `test_local.py`.

**Prueba de live y manejo de errores — 2026-05-04**
Se probó el live `https://www.youtube.com/watch?v=-MtbPcNE8ls` (`video_id=-MtbPcNE8ls`). Desde el entorno de ejecución, YouTube devolvió `429` en fetch web y `pytchat` falló con timeout SSL al iniciar handshake. Se añadieron callbacks `on_source_error`, `on_source_connect` y `on_source_disconnect` al `Aggregator`, y `YouTubeChatSource` notifica desconexión al terminar retries, para que la UI muestre errores de YouTube y restaure el botón de conexión si la red/YouTube rechaza la conexión.

**[WorkerSeniorAI]: Corrección `pytchat` en thread UI — 2026-05-04**
La UI reportó `signal only works in main thread of the main interpreter` al conectar el live desde RF3. Causa: `pytchat.create()` usa `interruptable=True` por defecto y registra `SIGINT`, lo cual falla fuera del hilo principal. Se agregó `source.interruptable: false` en `config/smart_aggregator.yaml` y `YouTubeChatSource` ahora llama `pytchat.create(video_id=..., interruptable=False)`. Verificado con creación de `pytchat` desde un thread de fondo y con `test_local.py`.

**[WorkerSeniorAI]: Filtro de emojis personalizados YouTube — 2026-05-04**
Durante la prueba real de chat se observaron aliases de emojis personalizados como `:bird:`, `:folded_hands:` y `:mending_heart:`. `MessageFilter` ahora detecta tokens `:alias:` como emojis personalizados: descarta mensajes compuestos solo por estos aliases y limpia los aliases cuando vienen mezclados con texto útil antes de enviar/loguear el mensaje filtrado. También se amplió la detección de menciones a `@[\w.-]+` para cubrir handles con guiones o puntos. `test_local.py` cubre emojis personalizados puros, limpieza de aliases y menciones con guion.

**[WorkerSeniorAI]: Ajuste operativo live real — 2026-05-04**
Con el chat real conectado se comprobó que `threshold_per_second: 10.0` era demasiado alto para pruebas funcionales: RF3 filtraba y calculaba vibe, pero no disparaba contexto agregado para Kira. Se ajustó `config/smart_aggregator.yaml` a `activity.threshold_per_second: 1.0`, `cooldown_seconds: 45.0` y `vibe.window_seconds: 60` para permitir reacciones en chats moderados sin spam. El prompt de vibe se hizo bilingüe/estricto JSON y la UI ahora muestra `note` cuando el vibe cae a fallback (`busy`, error LLM, respuesta vacía o parse error).

**[WorkerSeniorAI]: Diseño acordado anti-spam y UI YT Chat — 2026-05-04**
Tras validar comportamiento con chat real, el usuario eligió: pestaña separada `YT Chat`, respuesta automática con cooldown, resumen inteligente del flujo y un solo mensaje destacado, deduplicación por usuario, rate-limit por usuario y colapso visual de repetidos. Se agregó `spam` a `config/smart_aggregator.yaml` (`max_messages_per_user`, `user_window_seconds`, `duplicate_window_seconds`), `Aggregator` ahora filtra spam antes de callbacks/vibe/activity, la UI muestra mensajes en `YT Chat` y no en `Log General`, y `ui/app.py` incluye control `Max/u` para ajustar el límite de mensajes por usuario en runtime. El prompt de pico ahora instruye a Kira a responder al tema/energía general y no mensaje por mensaje.

**[WorkerSeniorAI]: Limpieza de logs operativos — 2026-05-04**
Se redujo ruido del `Log General`: la UI ya no marca timeouts/desconexiones de YouTube como error fatal, sino como aviso transitorio de reconexión; se evita loguear conexiones/desconexiones duplicadas al reconectar; el botón pasa por estado `Conectando...`; y los fallbacks de vibe se traducen a mensajes operativos (`Vibe omitido: Kira ocupada` o `Vibe no interpretable; usando neutral`) en lugar de exponer únicamente códigos internos como `fallback_due_to_parse_error`.

**[WorkerSeniorAI]: Prompt de respuesta natural — 2026-05-04**
Se ajustó el prompt enviado a Kira cuando RF3 detecta un pico de actividad. La versión anterior pedía inferir la “energía general del flujo”, y el modelo repetía esa formulación de forma técnica. Ahora el prompt pide una reacción natural y directa de co-host, prohíbe mencionar frases como `energia del flujo`, `mensaje destacado`, `contexto reciente` o `chat activo`, y limita la respuesta a 1-2 frases cortas con personalidad de Kira.

---

## Orden de Implementación Sugerido

1. `session_history.py` — base de todo (persistencia)
2. `message_filter.py` — el más sencillo, validar primero
3. `chat_source.py` — YouTube connection
4. `vibe_thermometer.py` — integrar con LLM compartido
5. `activity_trigger.py` — requiere 2 y 3
6. `aggregator.py` — orquesta todo con callbacks
7. `config.yaml` — unificar configuración
8. `test_local.py` — pruebas con datos mock

---

## 🧪 Escenarios de Prueba (test_local.py)

El archivo `smart_aggregator/test_local.py` debe permitir pruebas headless (sin UI, sin YouTube API).

### Datos Mock Comunes

```python
MOCK_MESSAGES_20 = [
    {"user": "user1", "text": "¡Hola Kira!", "timestamp": time.time()},
    {"user": "user2", "text": "GG 🔥", "timestamp": time.time()},
    {"user": "user3", "text": "¿Cómo estás?", "timestamp": time.time()},
    # ... 20 mensajes variados
]

MOCK_MESSAGES_200 = [...]  # 200 mensajes con variedad: cortos, largos, emojis, enlaces, menciones

VIBE_TEST_MESSAGES = [
    {"user": "fan1", "text": "ESTO ES INCREÍBLE 🔥🔥🔥", "timestamp": time.time()},
    {"user": "fan2", "text": "Qué momento épico", "timestamp": time.time()},
    {"user": "hater1", "text": "Esto es basura", "timestamp": time.time()},
    {"user": "normal1", "text": "Kira qué opinas del juego?", "timestamp": time.time()},
]
```

### TC3.1 — Filtro de Mensajes (RF3.1)

**Objetivo:** Verificar que el MessageFilter descarta correctamente.

| # | Descripción | Input | Esperado |
|---|---|---|---|
| TC3.1.1 | Descarta mensaje corto (< min_words) | "hola" | Descartado (None) |
| TC3.1.2 | Descarta emojis puros | "🔥🔥🔥" | Descartado |
| TC3.1.3 | Descarta enlaces | "mira esto https://youtube.com..." | Descartado |
| TC3.1.4 | Descarta menciones | "@Kira eres genial" | Descartado |
| TC3.1.5 | Pasa mensaje normal | "Kira qué juego estás jugando?" | Pasa |
| TC3.1.6 | VIP salta filtro | usuario en whitelist | Pasa aunque sea corto |
| TC3.1.7 | 200 mensajes variados | MOCK_MESSAGES_200 | Solo pasan los válidos |

```python
def test_tc3_1_filter():
    filter_cfg = load_config()["filter"]
    msg_filter = MessageFilter(filter_cfg)

    # TC3.1.1
    result = msg_filter.filter({"user": "a", "text": "hola", "timestamp": time.time()})
    assert result is None, "TC3.1.1: 'hola' debería ser descartado"

    # TC3.1.2
    result = msg_filter.filter({"user": "a", "text": "🔥🔥🔥", "timestamp": time.time()})
    assert result is None, "TC3.1.2: Emojis puros deberían ser descartados"

    # TC3.1.7: 200 mensajes
    passed = [m for m in MOCK_MESSAGES_200 if msg_filter.filter(m) is not None]
    print(f"TC3.1.7: De 200 mensajes, {len(passed)} pasaron el filtro")
    assert len(passed) > 0, "TC3.1.7: Alguno debería pasar"
```

### TC3.2 — Vibe Thermometer (RF3.2)

**Objetivo:** Verificar que el VibeThermometer analiza correctamente 20-200 mensajes.

| # | Descripción | Input | Esperado |
|---|---|---|---|
| TC3.2.1 | Ventana vacía | [] | Error o temperatura 0 |
| TC3.2.2 | 20 mensajes neutrales | MOCK_MESSAGES_20 | temperature ~50, emotions neutral alto |
| TC3.2.3 | 200 mensajes con hype | MOCK_MESSAGES_200 (filtrados) | excitement > 0.6 |
| TC3.3.4 | Ventana de 120s | mensajes con timestamps variados | compute_vibe solo al final de ventana |

```python
def test_tc3_2_vibe():
    vibe_cfg = load_config()["vibe"]
    llm_mock = lambda prompt: {"emotions": {"excitement": 0.7, "neutral": 0.2, "sadness": 0.1}, "temperature": 75}

    thermometer = VibeThermometer(vibe_cfg, llm_interface=llm_mock)

    for msg in VIBE_TEST_MESSAGES:
        thermometer.add_message(msg)

    vibe = thermometer.compute_vibe()
    assert vibe["temperature"] > 0, "TC3.2.1: Ventana no vacía"
    assert vibe["emotions"]["excitement"] > 0.5, "TC3.2.3: Hype detectado"
```

### TC3.3 — Activity Trigger (RF3.3)

**Objetivo:** Verificar detección de picos de actividad.

| # | Descripción | Input | Esperado |
|---|---|---|---|
| TC3.3.1 | Rate bajo | 5 msg en 5s | No trigger |
| TC3.3.2 | Rate supera umbral | 15 msg en 5s | Trigger callback llamado |
| TC3.3.3 | Auto reply configurado | threshold breached | callback recibe mensaje predefinido |
| TC3.3.4 | Rate vuelve a normal | dopo trigger, baja rate | No más triggers hasta nuevo pico |

```python
def test_tc3_3_activity():
    activity_cfg = load_config()["activity"]
    triggered = []

    def on_trigger(data):
        triggered.append(data)

    activity = ActivityTrigger(activity_cfg, callbacks={"on_trigger": on_trigger})

    # TC3.3.1: rate bajo
    for i in range(5):
        activity.on_message({"user": f"u{i}", "text": "msg", "timestamp": time.time()})
    time.sleep(0.1)
    assert len(triggered) == 0, "TC3.3.1: No debe triggerear con rate bajo"

    # TC3.3.2: rate alto
    for i in range(15):
        activity.on_message({"user": f"u{i}", "text": "msg", "timestamp": time.time()})
    time.sleep(0.1)
    assert len(triggered) > 0, "TC3.3.2: Debe triggerear con rate alto"
```

### TC3.4 — Session History (RF3.4)

**Objetivo:** Verificar persistencia híbrida SQLite + JSONL.

| # | Descripción | Esperado |
|---|---|---|
| TC3.4.1 | Crear sesión nueva | session_id > 0 en SQLite |
| TC3.4.2 | Guardar 20 mensajes | 20 registros en tabla messages |
| TC3.4.3 | JSONL contiene mismo msgs | Archivo existe y tiene líneas |
| TC3.4.4 | get_session_context retorna correctos | max_messages limita resultado |
| TC3.4.5 | cleanup_old_sessions borra viejos | Sesiones > retention_hours eliminadas |

```python
def test_tc3_4_history():
    import tempfile, os
    db_fd, db_path = tempfile.mkstemp()
    jl_fd, jl_path = tempfile.mkstemp()
    os.close(db_fd); os.close(jl_fd)

    history = SessionHistory(db_path, jl_path, retention_hours=1)

    sid = history.start_session("youtube", "test_channel")
    for i in range(20):
        history.add_context_snapshot(sid, f"contexto compacto {i}", message_count=i)

    context = history.get_recent_context_snapshots(sid, max_items=10)
    assert len(context) == 10, "TC3.4.4: Limita snapshots compactos"

    os.unlink(db_path); os.unlink(jl_path)
```

### TC3.5 — YouTube Chat Source (RF3.5)

**Nota:** Sin API key real, solo tests de interfaz y manejo de errores.

| # | Descripción | Esperado |
|---|---|---|
| TC3.5.1 | Connect sin video_id | Exception o error claro |
| TC3.5.2 | Connect con video_id inválido | Error handled gracefully |
| TC3.5.3 | is_connected antes de connect | False |
| TC3.5.4 | Disconnect sin connect previo | No crash |
| TC3.5.5 | Config de API key ausente | Error claro en logs |

```python
def test_tc3_5_chat_source():
    source = YouTubeChatSource(load_config()["source"], callbacks={})

    # TC3.5.3
    assert not source.is_connected(), "TC3.5.3: No conectado inicialmente"

    # TC3.5.1 y TC3.5.2: manejo de errores sin API real
    try:
        source.connect("invalid_video_id_123")
    except Exception as e:
        assert "api" in str(e).lower() or "auth" in str(e).lower(), "TC3.5.5: Error claro por falta de API key"
```

### TC3.6 — Aggregator Orchestration (RF3.x completo)

**Objetivo:** Probar el Aggregator orchestration con 200 mensajes simulados.

| # | Descripción | Esperado |
|---|---|---|
| TC3.6.1 | 20 mensajes → algunos filtrados | callback solo con msgs que pasan |
| TC3.6.2 | 200 mensajes → vibe calculado | on_vibe_update llamado al menos 1 vez |
| TC3.6.3 | 200 mensajes con spike → trigger | on_activity_trigger llamado |
| TC3.6.4 | Sesión iniciada y cerrada | SQLite + JSONL creados |
| TC3.6.5 | Todos los callbacks opcionales | No falla si callback es None |

```python
def test_tc3_6_aggregator_full():
    agg = Aggregator(config_path="config/smart_aggregator.yaml")
    filtered_msgs = []
    vibes = []
    triggers = []

    agg.on_filtered_message = lambda m: filtered_msgs.append(m)
    agg.on_vibe_update = lambda v: vibes.append(v)
    agg.on_activity_trigger = lambda d: triggers.append(d)

    # TC3.6.1-3: Simular 200 mensajes
    for msg in MOCK_MESSAGES_200:
        agg.process_message(msg)

    assert len(filtered_msgs) > 0, "TC3.6.1: Algún mensaje pasó"
    # TC3.6.2 y TC3.6.3 dependen de timing y config
```

### TC3.7 — YouTube API: Título y Categoría (RF4.x)

**Nota:** Requiere `YOUTUBE_API_KEY` en config. Sin key real, verificar que el código está preparado.

```python
# Config placeholder
youtube_api:
  api_key: "${YOUTUBE_API_KEY}"  # variável de ambiente
  channel_id: "${YOUTUBE_CHANNEL_ID}"

def test_tc3_7_youtube_api():
    cfg = load_config()["youtube_api"]
    if cfg.get("api_key") == "${YOUTUBE_API_KEY}":
        pytest.skip("YouTube API key no configurada")

    from smart_aggregator.youtube_api import YouTubeAPIClient
    client = YouTubeAPIClient(cfg["api_key"])

    # TC3.7.1: Obtener video actual
    video = client.get_live_video(cfg["channel_id"])
    assert video is not None, "TC3.7.1: Video en vivo encontrado"

    # TC3.7.2: Cambiar título
    new_title = "🔴 [EN VIVO] Kira Gaming - ¡Jugando con el chat!"
    result = client.update_video_title(video["id"], new_title)
    assert result is True, "TC3.7.2: Título actualizado"

    # TC3.7.3: Cambiar categoría
    # Game category ID (ej: 20 - Gaming)
    result = client.update_video_category(video["id"], category_id="20")
    assert result is True, "TC3.7.3: Categoría actualizada"
```

**Categorías comunes de YouTube:**
- 20 - Gaming
- 10 - Music
- 22 - People & Blogs
- 24 - Entertainment
- 17 - Sports

---

## 📋 Checklist de Prueba Pre-Deployment

- [x] TC3.1.1 - TC3.1.7: Filtro funcionando
- [x] TC3.2.1 - TC3.2.4: Vibe Thermometer sin nuevos LLM
- [x] TC3.3.1 - TC3.3.4: Activity Trigger preciso
- [x] TC3.4.1 - TC3.4.5: Persistencia híbrida funcional
- [x] TC3.5.1 - TC3.5.5: Chat Source graceful sin API real
- [x] TC3.6.1 - TC3.6.5: Aggregator orchestra correctamente
- [ ] TC3.7.1 - TC3.7.3: YouTube API (pendiente de API key real)
- [x] `core/llm_engine.py` / `motor_ia.py` no se modificó
- [x] `ui/app.py` solo contiene integración autorizada por el usuario: instanciación RF3, callbacks, adapter LLM silencioso y UI de YouTube
- [x] `python -c "from smart_aggregator import Aggregator; print('Import OK')"` funciona
- [x] `python smart_aggregator/test_local.py` pasa sin errores

---

## [WorkerSeniorAI] Cierre Funcional RF3 — 2026-05-04

### Estado Final

RF3 Smart Aggregator quedó funcional para uso real con YouTube Live en modo integrado con OpenCohost. El camino actual es correcto: RF3 opera como módulo independiente, el core LLM/TTS no fue modificado, y la UI solo actúa como capa de conexión, visualización y callbacks.

El sistema ya puede:

- Conectar a un chat de YouTube Live mediante URL o `video_id`.
- Mostrar mensajes filtrados en pestaña separada `YT Chat`.
- Mantener `Log General` limpio con eventos operativos solamente.
- Filtrar mensajes cortos, links, menciones, emojis Unicode puros y aliases de emojis personalizados tipo `:bird:`.
- Limpiar aliases de emojis personalizados cuando vienen mezclados con texto útil.
- Deduplicar spam repetido del mismo usuario.
- Aplicar rate-limit configurable por usuario desde UI (`Max/u`).
- Guardar historial por sesión en SQLite + JSONL.
- Calcular vibe por ventana usando una sola inferencia LLM inyectada, sin cargar otro modelo.
- Omitir vibe si Kira está ocupada para no competir con el pipeline principal.
- Detectar picos de actividad por mensajes/segundo.
- Enviar a Kira un contexto agregado de chat cuando hay pico, no cada mensaje individual.
- Hacer que Kira responda automáticamente con cooldown y estilo natural de co-host.

### Funcionalidades Implementadas

**RF3.1 — Filtro de Calidad**

- `MessageFilter` filtra por `min_words`, `min_char_length`, links, menciones y emojis puros.
- Whitelist configurable para usuarios VIP/mod.
- Soporte adicional real-world para aliases de emojis de YouTube (`:bird:`, `:folded_hands:`, `:mending_heart:`).
- Menciones con guiones/puntos (`@user-name`, `@user.name`) quedan cubiertas.

**RF3.2 — Vibe Thermometer**

- `VibeThermometer` acumula mensajes filtrados por ventana (`window_seconds`).
- Ejecuta como máximo una inferencia LLM por ventana.
- No importa ni llama Ollama directamente.
- Requiere `llm_interface` inyectada desde `ui/app.py`.
- Si Kira está ocupada, devuelve fallback neutral y lo reporta como evento operativo.
- Si el LLM no devuelve JSON válido, usa neutral y registra que el vibe no fue interpretable.

**RF3.3 — Activity Trigger**

- `ActivityTrigger` mide mensajes/segundo con timestamps reales.
- El umbral actual de prueba/operación es `1.0 msg/s`.
- Cooldown actual: `45s` para evitar spam de respuestas.
- El trigger se alimenta solo de mensajes aceptados tras filtros y anti-spam.

**RF3.4 — Session History**

- `SessionHistory` persiste sesiones en SQLite.
- JSONL mantiene audit trail.
- Cleanup por retención elimina SQLite y JSONL usando los mismos `session_id`.
- Guarda mensaje original, usuario, timestamp, estado de filtro y vibe.

**RF3.5 — YouTube Chat Source**

- `YouTubeChatSource` usa `pytchat`.
- `pytchat` corre en thread con `interruptable=False` para evitar errores de `signal`.
- Soporta reconexión con retries.
- Expone callbacks de conexión, error y desconexión.
- Errores transitorios de YouTube se muestran como avisos de reconexión, no como fallos fatales.

**Integración UI Autorizada**

- `ui/app.py` instancia `Aggregator`.
- Agrega campo para URL o `video_id`.
- Agrega botón conectar/desconectar chat.
- Agrega pestaña `YT Chat`.
- Agrega campo `Max/u` para rate-limit por usuario.
- Inyecta `llm_interface` silenciosa usando el modelo activo de `MotorVocalIA`.
- Inyecta `busy_callback` para omitir vibe si Kira está procesando o hablando.
- Envía contexto a Kira solo cuando hay pico de actividad.

### Comportamiento Esperado

**Conexión normal**

```text
[SmartAggregator] RF3 listo. Ingresa un video_id/URL de YouTube Live para conectar chat.
[SmartAggregator] Conectando chat YouTube: <video_id>
[SmartAggregator] Chat YouTube conectado: <video_id>
```

**Mensajes de YouTube**

- Deben aparecer en la pestaña `YT Chat`.
- No deben aparecer mensaje por mensaje en `Log General`.
- Mensajes spam repetidos del mismo usuario deben ocultarse/reducirse.

**Pico de actividad**

```text
[SmartAggregator] Pico de actividad detectado: 1.20 msg/s
[SmartAggregator] Contexto agregado enviado a Kira.
[IA] Analizando contexto con llama3...
```

Kira debe responder como co-host, directo y natural. No debe decir frases técnicas como `energía del flujo`, `mensaje destacado`, `contexto reciente` o `estoy analizando`.

**Vibe válido**

```text
[SmartAggregator] Vibe: 72/100 (excitement)
```

**Vibe omitido porque Kira está ocupada**

```text
[SmartAggregator] Vibe omitido: Kira ocupada.
```

**Vibe no interpretable**

```text
[SmartAggregator] Vibe no interpretable; usando neutral (fallback_due_to_parse_error).
```

Esto no rompe RF3. Solo significa que el LLM no devolvió JSON válido para el termómetro emocional y se usó neutral `50/100`.

**Error transitorio de YouTube**

```text
[SmartAggregator] Aviso YouTube: reconectando por fallo transitorio (...)
```

Esto es esperado con `pytchat`/YouTube si hay timeout, rate-limit o desconexión del servidor.

**Desconexión tras retries**

```text
[SmartAggregator] Chat YouTube desconectado tras agotar reconexiones.
```

### Casos de Prueba Cubiertos

- Mensajes cortos descartados.
- Emojis Unicode puros descartados.
- Emojis personalizados `:alias:` puros descartados.
- Emojis personalizados mezclados con texto limpiados.
- Links descartados.
- Menciones descartadas, incluyendo handles con guiones.
- VIP whitelist salta filtros.
- Vibe con mock LLM retorna emociones y temperatura.
- Vibe vacío retorna temperatura `0`.
- Activity trigger no dispara con rate bajo.
- Activity trigger dispara con rate alto.
- Activity trigger incluye acciones configuradas.
- SessionHistory crea sesión y guarda mensajes.
- SessionHistory limita contexto por `max_messages`.
- Cleanup borra sesiones antiguas en SQLite y JSONL.
- YouTubeChatSource maneja `video_id` vacío.
- YouTubeChatSource no crashea al desconectar sin conexión.
- Aggregator orquesta filtros, vibe, triggers, historial y callbacks.
- UI compila con la integración RF3.

### Configuración Operativa Actual

```yaml
vibe:
  window_seconds: 60

activity:
  window_seconds: 5
  threshold_per_second: 1.0
  cooldown_seconds: 45.0

spam:
  enabled: true
  user_window_seconds: 30
  max_messages_per_user: 10
  duplicate_window_seconds: 20
```

Estos valores son adecuados para pruebas reales y streams con actividad moderada. En producción con chats grandes, se recomienda subir `threshold_per_second` para que Kira no intervenga demasiado.

### Próximos Pasos Recomendados

1. Probar RF3 durante una sesión real de 20-30 minutos y revisar frecuencia de respuestas.
2. Ajustar `threshold_per_second` según tamaño del chat real.
3. Ajustar `cooldown_seconds` para que Kira no interrumpa demasiado al streamer.
4. Mejorar el prompt de Kira por categoría de stream o juego.
5. Agregar selector UI para modo RF3: `Solo monitoreo`, `Manual`, `Automático`.
6. Agregar botón para limpiar pestaña `YT Chat` sin borrar historial persistido.
7. Agregar métricas visibles: mensajes aceptados, descartados, spam filtrado y rate actual.
8. Implementar TC3.7/RF futuro solo si se requiere modificar título/categoría de YouTube; eso requiere OAuth 2.0, no solo API key.
9. Considerar un modelo/prompt más estricto para vibe si `fallback_due_to_parse_error` ocurre frecuentemente.

### Criterio de Cierre

RF3 se considera completado satisfactoriamente como Smart Aggregator funcional integrado a OpenCohost para lectura, filtrado, persistencia, análisis, detección de picos y respuestas automáticas controladas al chat de YouTube Live.

Queda fuera del cierre actual la administración de metadata de YouTube (`TC3.7`) porque pertenece a una integración API/OAuth distinta.
