# Instrucciones para Agente IA — RF3 Smart Aggregator

## ⚠️ Entorno Obligatorio

**Ejecutar todo con:**
```bash
E:\Miniconda\envs\flux_env\python.exe
```

**PROHIBIDO:**
- ❌ `pip install` / `pip uninstall` / `conda install`
- ❌ Modificar, actualizar o eliminar paquetes del entorno `flux_env`
- ❌ Usar `python` sin la ruta completa (puede apuntar a otro env)
- ❌ Crear requirements.txt o modificar dependencias existentes

Si necesitas una dependencia nueva, **consulta primero** — no instales nada por cuenta propia.

---

## Meta
Implementar el módulo `smart_aggregator/` completo según `docs/RF3_Smart_Aggregator_Spec.md`.

---

## Contexto del Sistema

El proyecto tiene **dos aplicaciones** que corren en paralelo:

1. **`VoiceAI`** (este proyecto) — UI en `ui/app.py`, motor IA, TTS, LLM
2. **`LiveAudio`** — App separada que se encarga de:
   - Escuchar audio del micrófono
   - Hacer transcripción con **Whisper**
   - Detectar voz con **Silero VAD**
   - Enviar transcripciones via WebSocket a VoiceAI

El **Smart Aggregator** (RF3) consume chat de **YouTube Live**, NO audio de LiveAudio. No necesita integrar con Silero ni Whisper. Solo recibe mensajes de chat ya formateados y los procesa con filtros, vibe thermometer y activity trigger.

---

## Paso 0 — Configuración Inicial

```bash
# 1. Crear y cambiar a rama feature/rf3-smart-aggregator
git checkout -b feature/rf3-smart-aggregator

# 2. Crear estructura de directorios
mkdir -p smart_aggregator data/smart_aggregator
touch smart_aggregator/__init__.py
```

---

## Paso 1 — Implementar en Orden

Seguir el orden de implementación del spec (sección "Orden de Implementación Sugerido"):

1. **`smart_aggregator/session_history.py`** — Persistencia híbrida SQLite + JSONL
2. **`smart_aggregator/message_filter.py`** — Filtros de calidad RF3.1
3. **`smart_aggregator/chat_source.py`** — Fuente YouTube con `pytchat`
4. **`smart_aggregator/vibe_thermometer.py`** — Análisis de sentimiento RF3.2
5. **`smart_aggregator/activity_trigger.py`** — Trigger por actividad RF3.3
6. **`smart_aggregator/aggregator.py`** — Orquestador principal
7. **`smart_aggregator/config.yaml`** — Configuración unificada
8. **`smart_aggregator/test_local.py`** — Tests headless con datos mock

---

## Paso 2 — Reglas de Implementación

### Arquitectura
- Cada clase en su propio archivo `.py`
- Todas接受 `config: dict` en `__init__` (cargado desde YAML)
- Interfaz de callbacks para comunicar con el core (ver spec)
- **NO crear nuevas instancias de LLM** — aceptar `llm_interface: callable` como parámetro

### Archivos a CREAR (solo estos)
```
smart_aggregator/
├── __init__.py
├── session_history.py
├── message_filter.py
├── chat_source.py
├── vibe_thermometer.py
├── activity_trigger.py
├── aggregator.py
├── config.yaml
└── test_local.py
```

### Archivos que NO se deben modificar
- `ui/app.py` ❌
- `motor_ia.py` ❌
- Cualquier otro archivo existente ❌

### Dependencias
- Solo usar: `pytchat` o `chatdownload`, `sqlite3` (stdlib), `pyyaml`, `requests`
- **NO usar:** `transformers`, `torch`, `tensorflow`, `nltk`, `textblob`, `vaderSentiment`

---

## Paso 3 — Tests

Ejecutar `test_local.py` después de cada clase implementada:

```bash
E:\Miniconda\envs\flux_env\python.exe smart_aggregator/test_local.py
```

Los tests deben cubrir los escenarios TC3.1 a TC3.7 del spec.

---

## Paso 4 — Actualizar Documentación

Al terminar cada clase, actualizar:

1. **`docs/RF3_Smart_Aggregator_Spec.md`** — Marcar como ✅ la clase implementada en la sección correspondiente
2. **`docs/changes.md`** — Conforme se completen RF3.1-RF3.5, marcar ✅ en la tabla

---

## Paso 5 — Verificación Final

Antes de reportar completion, verificar:

```bash
# 1. Import funcional
E:\Miniconda\envs\flux_env\python.exe -c "from smart_aggregator import Aggregator; print('Import OK')"

# 2. Tests pasan
E:\Miniconda\envs\flux_env\python.exe smart_aggregator/test_local.py

# 3. Ningún archivo existente modificado
git status
```

**[WorkerSeniorAI] Nota de revisión — 2026-05-04:** Los comandos de prueba fueron normalizados para usar siempre el intérprete obligatorio `E:\Miniconda\envs\flux_env\python.exe`. La implementación corregida mantiene RF3 fuera de `ui/app.py` y `motor_ia.py`; el core debe inyectar `llm_interface` si quiere análisis LLM real en `VibeThermometer`.

**[WorkerSeniorAI] Nota de integración autorizada — 2026-05-04:** El usuario autorizó integrar la opción 2 en `ui/app.py`. Esta integración solo instancia y cablea callbacks/adapters: `Aggregator` recibe `llm_interface`, `busy_callback`, controles de URL/`video_id` de YouTube Live y callbacks de log/contexto. `core/llm_engine.py` permanece sin cambios.

---

## Commit Message Format

```
feat(smart-aggregator): implement <feature>

- <RF3.X> <short description>
- <RF3.X> <short description>

Refs: docs/RF3_Smart_Aggregator_Spec.md
```

Ejemplo:
```
feat(smart-aggregator): implement session_history and message_filter

- RF3.1 MessageFilter with configurable thresholds
- RF3.4 SessionHistory with SQLite + JSONL persistence

Refs: docs/RF3_Smart_Aggregator_Spec.md
```

---

## Communication Contract with Core (ui/app.py)

El aggregador se instancia en `ui/app.py` así (código de referencia, NO modificar):

```python
from smart_aggregator import Aggregator

self.smart_agg = Aggregator(config_path="config/smart_aggregator.yaml")
self.smart_agg.on_filtered_message = self._on_kira_input  # mensaje filtrado → pipeline Kira
self.smart_agg.on_vibe_update = self._update_vibe_display  # actualización de temperatura
self.smart_agg.on_activity_trigger = self._handle_activity_trigger  # pico de chat
```

**El módulo solo define los callbacks. El core decide qué hacer con ellos.**

---

## Recursos

- Spec completo: `docs/RF3_Smart_Aggregator_Spec.md`
- Casos de prueba: misma sección en spec (TC3.1 — TC3.7)
- Configuración ejemplo en spec bajo cada clase
