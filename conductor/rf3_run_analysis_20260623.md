# RF3 "Chat Live" Run Analysis — 2026-06-23

**Method:** 29-agent Opus workflow. 6 parallel decomposition lenses (38 findings) →
per-track validate→adversarial-refute pipeline over 11 session tracks → single Opus
synthesis. Every claim grounded in a log timestamp and/or a live `file:line`.

**Run under test:** ~56 min RF3 `_build_chat_live_tab` ONLY (no cohost, no PTT, no
operator talking to Kira). Pure Kira reacting to a Spanish-region Twitch streamer's chat.
Model `llama3`, profile Akira (System Role=True), locale `es`. (Streamer name intentionally
not recorded.)

---

## 1. Veredicto de la corrida

NO está stream-ready. Los primeros 11-12 minutos fueron buenos —variada, en personaje, con
voseo presente ("Vos sabés que la ilusión es la mejor medicina para este stream", 09:29-09:40)—
pero a partir de ~09:43 Kira colapsó durante **~40 minutos seguidos** en una plantilla fija
("¡La verdad es que [NOUN] es un [METAPHOR]!") con emisiones VERBATIM repetidas (la misma línea
"¡La verdad es que la sorpresa es un regalo!" CUATRO veces exactas en ~3 min: 09:43:30 /
09:44:41 / 09:45:45 / 09:46:53). Y lo más grave para la salud de la plataforma: **ningún
guardrail de similitud disparó en 56 minutos** —el detector existe, pero no está cableado en el
path que el streamer realmente usó—. Un co-host que repite la misma frase verbatim cuatro veces
frente a la audiencia y nadie lo corta no es presentable. El loop, no la latencia ni el TTS, es
el motivo del NO.

## 2. Qué falló, ranqueado

### CRITICAL — El loop de repetición (el titular)

El mecanismo NO es un solo bug, son **tres causas que se potencian**, todas en el path
`source="chat"`:

1. **Sampling sin freno anti-repetición.** `opciones_llm` en `llm_engine.py:1035-1040` setea SOLO
   `temperature`/`top_p`/`num_predict`/`num_ctx`. No hay `repeat_penalty`, ni `frequency_penalty`,
   ni `presence_penalty`. Ollama cae a su default `repeat_penalty=1.1` sobre `repeat_last_n=64`
   tokens —una ventana demasiado chica para abarcar varios turnos cortos, así que no puede castigar
   una plantilla re-emitida turno tras turno—. Con `LLM_TEMPERATURE=0.8` / `LLM_TOP_P=0.9`
   (`settings.py:36-38`) el modelo vaga lo justo para encontrar un atractor de alta probabilidad y
   después nada lo empuja afuera. (REP-1, conf 0.9)

2. **El guardrail de similitud NUNCA corre en chat.** Toda la familia `is_too_similar_to_recent` /
   `is_repetition` / `has_looping_lines` / `ERR_GUARDRAIL_SIMILAR` vive SOLO en
   `kira_agenda_controller.py:1049-1093` y está cableada vía `app_shell.py:195`, que
   `_generar_dialogo` invoca ÚNICAMENTE bajo `if source.startswith("kira-agenda")`
   (`llm_engine.py:1170`). El path de chat (`source="chat"`) pasa solo por `output_guard`
   (`validation.py:352-407`), que es matching de contenido (auto-ID de IA, meta-comentario,
   engagement negativo, promesas) **con CERO lógica de repetición**. Por eso el único trip en 56 min
   fue `no_negative_engagement` y nunca uno de similitud: el detector físicamente no se ejecutó sobre
   estas salidas. (REP-2 / GRD-1 / MDL-1, conf 0.9-0.93)

3. **El historial se auto-refuerza.** Cada respuesta de Kira se commitea al deque
   (`llm_engine.py:1419-1420`) y se re-inyecta en el prompt del siguiente turno
   (`llm_engine.py:978-991`). La plantilla se auto-alimenta. **Prueba decisiva**: "Historial de
   conversación limpiado" a las 09:30:43 vació el deque y produjo frescura momentánea... y volvió a
   colapsar a los ~13 min. Eso es la firma textbook de un atractor in-context, no de overflow.
   (REP-4 / MDL-3, conf 0.8-0.82)

**Importante (REP-3 / GRD-3)**: aunque cablearas el detector de agenda tal cual al chat, NO
atraparía el colapso dominante. `is_too_similar_to_recent` exige overlap de tokens ≥0.78 sobre
tokens de 4+ caracteres; el synonym-swap ("confusión/ironía/abismo/laberinto…") comparte tan pocos
tokens de contenido que cae bajo 0.78. Solo `is_repetition` (match exacto) atraparía las 4×
verbatim. Para la plantilla con relleno rotativo hace falta un detector
**estructural/scaffold/n-gram de apertura**, no Jaccard de tokens.

### HIGH — TTS: fragmento dropeado sin fallback (TTS-1, conf 0.9)

A las **10:00:44** Edge-TTS levantó `NoAudioReceived` ("No audio was received"). El clasificador
`_is_connection_error()` (`llm_engine.py:46-67`) solo reconoce `socket.gaierror`, `ssl.SSLError` y
`aiohttp.ClientConnectorError`. `NoAudioReceived` hereda de `EdgeTTSException -> Exception` directo,
NO está en el allowlist, devuelve `False`, cae a la rama "Edge-TTS requiere internet"
(`llm_engine.py:1792`), dropea el chunk (**0/1 fragmentos**) y NO invoca Piper aunque Piper estaba
cargado. Es un hueco de cobertura del gate `local_light_tts_piper`: cubre offline/DNS/SSL pero no
"conectó-pero-respondió-vacío".

### MEDIUM — Latencia TTS con picos de dead air (TTS-4, conf 0.75)

First-fragment normalmente 0.7-1.4s, pero saltó a **10.20s (10:03:46)** y **11.78s (10:08:13)** →
silencio audible; totales de pipeline 14.3s/15.5s. Es el round-trip de red bloqueante de Edge-TTS
`Communicate.save()` (`llm_engine.py:1710-1714`), bien por debajo de los 45s de
`TTS_LIGHT_TIMEOUT`. Con el model trace constante (sin tier churn), el LLM NO es la causa: es
varianza del CDN de Azure. Hipótesis, no afirmación.

### LOW — Persona/voseo y warning de coherencia (PVL-4 / PVL-6 / REP-6)

El voseo se desvanece tras ~09:43 porque la plantilla colapsada ("X es un Y") es 3ª persona neutra:
no hay verbos de 2ª persona donde inflexionar. Es **síntoma del colapso, no un bug de i18n
independiente**. El warning de arranque `profile_persona_ungoverned` es real y correcto pero
warn-only (`coherence.py:107`, la persona siempre gana) — confunde el análisis del idioma, no causa
el loop. Detalle de diseño: Akira es rioplatense (es-AR) sirviendo a un chat de España (es-ES);
`primary_subtag` colapsa ambos a "es", así que ni el gate preciso detectaría el mismatch regional.

### POSITIVE — El output_guard + fallback neutral funcionó (GRD-2 / REP-7, conf 0.85-0.92)

A las **10:18:09**, `no_negative_engagement` BLOQUEÓ en `layer=output_guard`, sustituyó por línea
neutral rotativa SIN llamar al LLM (`_guardrail_fallback_line`, `llm_engine.py:950-961`), sintetizó
y reprodujo bien. La maquinaria de recuperación EXISTE y está sana en el chat path — es exactamente
el punto de inserción natural para un guard de repetición.

## 3. Matriz de tracks: validados / invalidados

| Track | Veredicto (post-refute) | ¿Arregla algo observado? | Prioridad | Confianza | Por qué (atado a evidencia) |
|---|---|---|---|---|---|
| Cohost Repetition Handling (detect→trim→regenerate) | PARTIALLY_VALIDATED | No (tal como está escrito) | **P1** | 0.85 | Mecanismo correcto para el #1 síntoma, pero gateado a `kira-agenda` (`llm_engine.py:1170`) — nunca tocó `source="chat"`. Requiere extender al chat + detector scaffold para el synonym-swap. |
| Model Qualification + Mini-Benchmark (ADR-014) | PARTIALLY_VALIDATED | No | **P3** | 0.78 | D1 (reasoning-cap) hecho; D2 (mini-bench) sin implementar. Solo re-confirmaría lo que ADR-015 ya dijo (llama3 loopea) y no BLOQUEA selección (D3/D4). Advisory upstream. |
| Kira Conversational Memory (history summarization) | NOT_EXERCISED | No | P3 | 0.85 | Digest solo se inyecta en `source="direct"` (`llm_engine.py:980/1019`); en chat `digest_block=""`. Compacta turnos YA evictados, no el deque vivo que causa el loop. |
| Context-Window Overflow Guardrail (2h+) | **INVALIDATED** | No | P3 | 0.9 | Deque hard-bounded de 20 msgs (`llm_engine.py:149`), input ~1200 de ~3328 tokens. Overflow aritméticamente imposible. Colapso al min 15, no a las 2h. Ningún layer del track se activa. |
| Profile-Language Auto-Detect | **INVALIDATED** | No | NONE | 0.9 | Solo detecta/gobierna, nunca enforza voseo. `primary_subtag` colapsa es-AR/es-ES a "es" → cero output este run. La deriva de voseo es síntoma del colapso. |
| Engine Locale Residue (i18n) | NOT_EXERCISED | No | P3 | 0.9 | Run fue locale=es; bajo es el track retorna los strings legacy byte-idénticos. No-op por construcción. Sin implementar ([~] design). |
| Latency Tracing Debug Mode | NOT_EXERCISED | No | P3 | 0.83 | Tracer solo arranca en PTT/LiveVoice STT; RF3 chat nunca llama `begin_turn`. `finish()` corta en `_in_turn=False`. Cero líneas `[LATENCY]`. Gap real: ciego al path más común. |
| Status Bars Stale State | NOT_EXERCISED | No | NONE | 0.88 | Sin model-switch fail, sin agenda PAUSED, sin health-red. Ninguna precondición ocurrió. |
| App Startup Clarity | NOT_EXERCISED | No | NONE | 0.95 | Arranque limpio ~14s; los 56 min son steady-state. El track gobierna solo la ventana de warm-up. |
| Repo Hygiene Audit (H3) | **INVALIDATED** | No | P3 | 0.95 | De-dup de constantes en el pipeline agenda/topic, nunca ejercitado. "No runtime behavior change" por diseño. |
| Reasoning-Model Token Budget (DONE) | NOT_EXERCISED | No | NONE | 0.93 | llama3 reporta `thinking=False`; sin eventos empty-output. El síntoma es lo OPUESTO (sobre-producción). Control negativo. |

## 4. Las opciones más óptimas

Secuencia recomendada, por palanca-por-unidad-de-esfuerzo:

**PRIMERA LÍNEA (máxima palanca, mínimo esfuerzo) — el sampling fix.** Agregar `repeat_penalty`
(≈1.15-1.3), `presence_penalty` y/o `frequency_penalty` a `opciones_llm` en `llm_engine.py:1035-1040`.
Es **model-independent**, de una línea conceptual, ataca directamente el atractor que ALIMENTA las
duplicaciones verbatim, y no toca arquitectura. Esta es la mejor relación palanca/esfuerzo y debería
ser lo primero. Es el lever que el track formal de repetición NO cubre (la escalera detecta/regenera
DESPUÉS; esto evita que el modelo entre al loop de entrada).

**SEGUNDA — Wirear la escalera detect→trim→regenerate al path de chat (track P1).** Extender el guard
del `source="kira-agenda"` a `source="chat"`, reutilizando la maquinaria de `_guardrail_fallback_line`
que ya vive en el chat path (`llm_engine.py:950-961`). PERO con una salvedad NO opcional: el detector
actual (`token-overlap ≥0.78`) atrapa solo las verbatim, NO el synonym-swap. Hay que sumarle un
**detector estructural/scaffold/n-gram de apertura** para plantillas con relleno rotativo. Sin eso,
cierra el subset verbatim y deja abierto el titular.

**TERCERA — Qualification de modelo (ADR-014 D2), advisory.** Útil para no promover modelos a live sin
full-stress, pero NO arregla esta corrida: es WARN, no bloquea selección, y su probe corto de
seed-fijo probablemente no reproduzca el atractor multi-turno. Prioridad baja relativa.

**El orden importa**: el sampling fix solo puede ser suficiente para esta corrida (hipótesis a validar
— ver sección 6). La escalera es el cinturón-y-tiradores que garantiza que NINGUNA repetición llegue
al TTS aunque el sampling falle. Hacé el #1 primero y medí; recién ahí decidís cuánto del #2 necesitás.

## 5. Gaps sin track

1. **Sampling-level anti-repetición (NO es la escalera).** El lever más barato del run NO tiene track
   propio: nadie cubre los penalties de Ollama en `opciones_llm`. La escalera detect/regenerate es
   reactiva (post-generación); esto es preventivo (durante el sampling). **Amerita track nuevo chico**
   o sumarse como un D extra al track de Repetition Handling — pero conceptualmente es distinto y no
   debe confundirse con la escalera.

2. **Drop de fragmento Edge-TTS sin fallback (TTS-1).** El gate `local_light_tts_piper` no clasifica
   `NoAudioReceived` como connection-error, así que dropea en vez de caer a Piper. Fix de bajo riesgo:
   agregar `edge_tts.exceptions.NoAudioReceived` (y posiblemente `aiohttp.ServerTimeoutError`) al set
   elegible de fallback en `_is_connection_error` (`llm_engine.py:46-67`), o un retry one-shot a Piper
   en la rama de drop (`llm_engine.py:1792-1797`) cuando Piper `is_available()`. **Entra en el track
   existente de TTS/Piper-fallback** como extensión de cobertura — no es regresión, es blind-spot
   as-built.

3. **Latencia TTS 10-11s de dead air (TTS-4).** Varianza exógena del CDN de Azure sobre el `save()`
   bloqueante serial, sin retry ni síntesis especulativa. **No amerita track propio urgente**; un
   warmup pre-síntesis o un race especulativo a Piper lo enmascararía, pero la latencia en sí es de
   red. Vinculado al gap #2 (mismo path bloqueante).

4. **Tracer ciego al path de chat-reaction (scope gap de Latency Tracing).** El tracer no tiene seam de
   entrada para `enqueue(source="chat")` — el modo más común según CLAUDE.md. Si el owner quiere datos
   de latencia del modo que realmente usa, hay que sumar un tercer seam. **Entra en el track existente
   de Latency Tracing** como extensión.

5. **Mismatch regional intra-idioma (es-AR vs es-ES).** `primary_subtag` trunca al primer subtag, así
   que ningún gate actual ni planeado detecta una persona rioplatense sirviendo audiencia peninsular.
   Gap de diseño real, fuera de scope de los tracks de locale actuales. Bajo, no bloqueante.

## 6. Qué necesita validación de runtime del owner

1. **¿`repeat_penalty` solo corta el loop?** Es LA pregunta de palanca. Hipótesis: agregar
   `repeat_penalty`/`presence_penalty` a `opciones_llm` puede bastar para esta corrida sin tocar la
   escalera. Correr otra sesión RF3 con llama3 + Akira con los penalties seteados y ver si el atractor
   de plantilla desaparece. Si desaparece, la escalera pasa a ser cinturón-y-tiradores (P1 sigue, pero
   baja urgencia). Si NO, confirma que se necesita el guard de similitud estructural sí o sí.

2. **¿`prompt_eval_count` se acercó a `num_ctx`?** No lo podemos saber: el código NUNCA lee
   `prompt_eval_count`/`eval_count` (`num_ctx=4096` es constante ciega en `llm_engine.py:1039`, el
   envelope se lee solo por `'message'` en `llm_engine.py:1096`). La aritmética dice ~0.3-0.4 de
   utilización, pero sin telemetría es inferencia. Instrumentar la lectura de `prompt_eval_count` —lo
   que el Layer 4 del track de overflow agregaría— en esta corrida habría DISPROBADO overflow
   positivamente.

3. **¿El detector token-overlap ≥0.78 atrapa el synonym-swap si se cablea a chat?** Hipótesis fuerte:
   NO. Validar con una corrida que el scaffold rotativo realmente cruza el umbral o no; si no lo cruza
   (lo esperado), justifica invertir en el detector estructural antes de declarar la escalera
   suficiente para chat.

4. **¿llama3 sobrevive un full-stress de 10 tópicos en el chat path con los fixes?** ADR-015 línea 55
   pedía el full-stress que nunca se corrió; esta sesión es el primer dato de duración real y falló.
   Una corrida post-fix es el gate para declarar (o no) a llama3 como pick live del chat path.

---

**Archivos clave referenciados** (todos en `E:\VoiceAI\`): `opencohost/core/llm_engine.py`
(opciones_llm `1035-1040`, gate agenda `1144`/`1170`, output_guard call `1159`, fallback `950-961`,
historial `978-991`/`1419-1420`, deque `149`, `_is_connection_error` `46-67`, drop branch
`1792-1797`, TTS save `1710-1714`), `opencohost/config/validation.py:352-407`,
`opencohost/config/settings.py:36-39`, `opencohost/smart_aggregator/kira_agenda_controller.py:1049-1093`,
`opencohost/ui/app_shell.py:195`, `opencohost/i18n/coherence.py:44-48`/`107`.

> Nota: los `file:line` provienen del análisis de los agentes contra el árbol vivo del branch
> `feat/akira-voseo-fix-and-cohost-adr`. Reconfirmá coordenadas antes de cualquier edición (el audit
> de diseño del 2026-06-23 ya documentó drift de file:line en este repo).
