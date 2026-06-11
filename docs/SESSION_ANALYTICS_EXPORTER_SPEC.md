# Session Analytics Exporter — Spec adicional no prioritario

Este documento define una idea futura para que OpenCohost genere analíticas locales de sesión sin consumirlas dentro de la app. La app solo debe producir datos; un dashboard/programa externo puede leerlos después bajo responsabilidad del streamer.

> Estado: **adicional / no prioritario / no implementar todavía**.
>
> Consulta subagente: se pidió revisión tipo DeepSeek/opencode-go. El runtime visible no permitió probar identidad de modelo; el subagente declaró explícitamente que no podía verificar DeepSeek desde su entorno.

## Decisión base

| Área | Decisión |
|---|---|
| Activación | Módulo global **OFF por defecto**. |
| UI | Nueva tab de configuración. Al activarlo, las categorías internas quedan ON por defecto. |
| Consumo | OpenCohost **no** muestra gráficos ni dashboard. Solo genera datos. |
| Storage | SQLite local mensual: `data/session_analytics/YYYY-MM.sqlite`. |
| Alcance | Plataforma + canal + sesión + métricas + chat post-filtro + highlights + prompt/profile history. |
| Raw chat | No guardar chat crudo completo. Guardar solo mensajes aceptados post-filtro y highlights seleccionados. |
| Prompt | Guardar nombre/perfil siempre; guardar prompt completo solo cuando cambia, con versión/hash. |
| Vectorial | No en MVP. Fase futura solo para summaries/topics/highlights. |

## Objetivos

- Generar una bitácora analítica local para entender qué ocurrió en una sesión.
- Ayudar a depurar el comportamiento de Kira sin depender de logs humanos enormes.
- Permitir que un dashboard externo cree reportes post-stream.
- Mantener el runtime de Kira, TTS, LLM, SmartAggregator y UI aislado de cualquier fallo analítico.

## No objetivos

- No crear gráficos dentro de OpenCohost.
- No usar estas analíticas como input directo para el LLM durante el stream.
- No guardar chat rechazado/raw completo.
- No crear una DB por sesión.
- No introducir una vector DB pesada en la primera versión.
- No reemplazar `stream_admin/analytics.py`, que hoy es un tracker liviano en memoria para contexto/UI.

## Datos a registrar

### Sesión

- `platform`: Twitch, YouTube, Kick, test, etc.
- `channel_id` / canal externo.
- nombre visible del canal.
- URL de sesión si existe.
- inicio/fin.
- versión de OpenCohost.
- configuración activa del exporter.

### Chat post-filtro

Guardar mensajes que ya pasaron el filtro operativo, con:

- timestamp relativo y absoluto.
- username real de la plataforma.
- texto post-filtro.
- política de filtro aplicada, por ejemplo `twitch_relaxed`.
- hash de mensaje.
- flags/calidad/reason si están disponibles.

Esto no equivale a “dato seguro”: el filtro reduce ruido, pero puede seguir habiendo toxicidad o datos sensibles. Por eso debe estar explicado en la UI.

### Highlights

Comentarios o eventos que el algoritmo seleccionó como relevantes:

- timestamp.
- username.
- texto literal del comentario destacado.
- razón: `high_relevance`, `repeated_theme`, `kira_reference`, `burst_anchor`, etc.
- score/confianza si existe.

### Métricas agregadas

- mensajes por segundo/minuto.
- mensajes vistos/aceptados/rechazados.
- razones de rechazo por ventana.
- densidad de emotes.
- duplicados/copypaste agrupado.
- usuarios destacados por participación/aceptación.
- temas frecuentes.
- palabras/tokens frecuentes.
- picos de actividad.

### Tópicos determinísticos

Los tópicos no deben depender únicamente de una inferencia LLM libre. Para que el dashboard externo pueda auditar y repetir resultados, el exporter debe guardar señales determinísticas por ventana de tiempo:

- tokens normalizados más frecuentes.
- n-grams frecuentes.
- hashes de mensajes repetidos.
- grupos de copypaste.
- usuarios participantes por tópico.
- mensajes aceptados que sirvieron como evidencia.
- ventana temporal exacta: `start_ts_ms` / `end_ts_ms`.
- política de filtro activa.

El LLM, si se usa, debe agregar una capa de resumen sobre esas señales, no reemplazarlas. La tabla `topics` puede guardar `summary`, pero debe existir evidencia trazable en `topic_evidence` o una estructura equivalente.

Tabla sugerida:

```txt
topic_evidence(id, topic_id, evidence_type, value, count, first_ts_ms, last_ts_ms, metadata_json)
```

Regla: dos ejecuciones sobre la misma sesión y configuración deberían producir los mismos buckets, conteos y evidencias determinísticas, aunque el resumen textual pueda variar si se regenera con LLM.

### Kira y rendimiento

- perfil activo.
- cambios de perfil.
- cambios de prompt.
- hash/version de prompt.
- prompt completo solo cuando cambia.
- respuestas de Kira por tipo: chat, agenda, fallback, cohost, etc.
- latencia LLM.
- latencia TTS.
- fallbacks TTS/LLM.
- errores recuperables.
- arranque de Ollama/Qwen/OBS.

## Arquitectura propuesta

```txt
OpenCohost runtime
  ↓ eventos mínimos
AnalyticsEventBus / queue bounded
  ↓
SessionAnalyticsWorker
  ↓ batch writes
SQLite mensual
  ↓
dashboard externo / herramienta del streamer
```

Reglas críticas:

- Nunca escribir desde el hilo UI.
- Nunca bloquear el pipeline de Kira por analytics.
- Cola con límite.
- Si la cola se llena, dropear eventos analíticos y contar drops.
- Si SQLite falla repetidamente, desactivar exporter para esa sesión.
- La app principal siempre gana.

## Storage sugerido

Ruta:

```txt
data/session_analytics/
  2026-05.sqlite
  2026-06.sqlite
```

### Tablas base

```txt
platforms(id, name)
channels(id, platform_id, external_id, display_name)
sessions(id, channel_id, started_at, ended_at, url, app_version, config_version)
users(id, platform_id, external_id, display_name)
accepted_chat(id, session_id, user_id, ts_ms, message_hash, text, filter_policy, flags_json)
message_groups(id, session_id, message_hash, normalized_text, first_ts_ms, last_ts_ms, repeat_count)
highlights(id, session_id, user_id, ts_ms, text, reason, score)
topics(id, session_id, start_ts_ms, end_ts_ms, summary, message_count, confidence)
topic_evidence(id, topic_id, evidence_type, value, count, first_ts_ms, last_ts_ms, metadata_json)
metrics(id, session_id, bucket_ts_ms, metric_name, value)
kira_events(id, session_id, ts_ms, event_type, metadata_json)
prompt_versions(id, session_id, profile_name, prompt_hash, prompt_version, prompt_text, changed_at_ms)
exporter_health(id, session_id, ts_ms, status, dropped_events, queue_size, last_error)
schema_migrations(version, applied_at)
```

### Índices mínimos

- `sessions(channel_id, started_at)`
- `accepted_chat(session_id, ts_ms)`
- `accepted_chat(message_hash)`
- `message_groups(session_id, message_hash)`
- `highlights(session_id, ts_ms)`
- `topics(session_id, start_ts_ms)`
- `topic_evidence(topic_id, evidence_type)`
- `topic_evidence(value)` si se requiere búsqueda rápida de tokens/hashes.
- `metrics(session_id, bucket_ts_ms, metric_name)`
- `prompt_versions(prompt_hash)`

FTS5 puede agregarse después para buscar en `accepted_chat.text`, `highlights.text` y `topics.summary`.

## Resiliencia de DB

- SQLite en WAL mode.
- `busy_timeout` bajo y controlado.
- Batch inserts cada N eventos o cada X segundos.
- Transacciones cortas.
- `INSERT OR IGNORE` / UPSERT donde aplique.
- Primary keys internas, nunca username como clave primaria.
- Deduplicación por `message_hash + session_id + time_bucket`.
- Health counters visibles en configuración: written, dropped, last_error, disabled_reason.
- Botón para pausar exporter.
- Botón/ruta clara para borrar datos locales.

Si una escritura falla:

1. registrar error en contador interno/log operativo.
2. continuar con el siguiente batch.
3. si supera umbral de fallos, desactivar analytics para esa sesión.
4. nunca lanzar excepción hacia UI/Kira.

## Coste esperado

### Rendimiento

Costo bajo si se respeta el diseño:

- contadores en memoria: despreciable.
- cola no bloqueante: bajo.
- batch SQLite: bajo/moderado.
- FTS5: moderado si se activa.
- embeddings: alto si se hacen en caliente; por eso quedan fuera del MVP.

### Espacio

Estimación orientativa:

- métricas solamente: pocos MB por mes.
- métricas + highlights + chat aceptado post-filtro: decenas a cientos de MB por mes según volumen.
- streams hostiles con muchos aceptados: puede crecer fuerte; controlar con deduplicación, límites y retención.

Controles:

- límite de chars por mensaje.
- agrupación de duplicados/copypaste.
- retención configurable por meses.
- `VACUUM`/compactación manual o programada.
- DB mensual para evitar una DB infinita.

## Fases sugeridas

### Fase 1 — MVP Analytics Core

- Nueva tab de configuración con `session_exporter.enabled = false`.
- Advertencia clara de qué se guarda.
- DB mensual.
- Event bus + queue bounded.
- Worker asíncrono.
- Registro de sesiones/plataforma/canal.
- Métricas por minuto.
- Chat aceptado post-filtro.
- Highlights literales.
- Tópicos determinísticos por ventana con evidencia trazable.
- Cambios de perfil/prompt versionados.
- Eventos de rendimiento LLM/TTS/fallback.
- Health counters del exporter.

### Fase 2 — Export y mantenimiento

- Export JSON/CSV para dashboard externo.
- FTS5 opcional.
- Retención configurable.
- Compactación/VACUUM.
- Migraciones versionadas.
- Herramienta de inspección mínima fuera de OpenCohost.

### Fase 3 — Semántica/vectorial

- Embeddings solo para:
  - summaries de sesión.
  - topics.
  - highlights.
  - resúmenes de respuestas de Kira.
- No embeddings por cada mensaje aceptado por defecto.
- Rebuild offline/manual o background con límites duros.

Tabla posible:

```txt
embeddings(id, entity_type, entity_id, model, vector_blob, created_at_ms)
```

## Criterios de aceptación

- Con exporter OFF, no se crea DB ni hay overhead apreciable.
- Con exporter ON, fallos de SQLite no interrumpen UI, Kira, TTS, LLM ni SmartAggregator.
- No se guarda chat raw completo ni chat rechazado.
- El chat guardado corresponde a mensajes post-filtro aceptados y highlights.
- Los tópicos tienen evidencia determinística auditable; el resumen LLM no es la única fuente de verdad.
- La DB mensual permite consultar por plataforma, canal y sesión.
- El prompt completo solo se guarda cuando cambia.
- El dashboard externo puede leer los datos sin depender del proceso de OpenCohost.
- Tests cubren: módulo apagado, cola llena, DB bloqueada, error de escritura, duplicados y cambio de prompt.

## Preguntas abiertas

- ¿Guardar prompt completo por defecto cuando analytics está ON, o hacerlo toggle separado?
- ¿Retención por defecto: 30, 60 o 90 días?
- ¿Límite por defecto de chars por mensaje aceptado?
- ¿FTS5 en MVP o Fase 2?
- ¿Export externo inicial en JSON, CSV o ambos?
- ¿Modo test/sandbox separado para no mezclar pruebas de múltiples canales con sesiones reales?

## Nota de implementación futura

Este módulo debe implementarse como track separado, por ejemplo `session-analytics-exporter`, después de cerrar la rama actual de resiliencia. No mezclar con fixes de Qwen/Ollama/SmartAggregator salvo que el track lo requiera explícitamente.
