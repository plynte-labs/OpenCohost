# Urgent Feature Test — Kira Chaos Stream Protocol

Este documento define el test urgente que falta antes de considerar VoiceAI listo para empaquetado: probar a Kira en condiciones caóticas reales de stream, sin depender de un live real ni de servicios externos cuando no sea necesario.

## Resultado esperado

VoiceAI debe demostrar que puede sostener agenda, chat reactivo e interrupciones del streamer sin responder tarde, sin repetir, sin perder prioridad humana y sin corromper estado interno.

## Estado

| Campo | Valor |
|---|---|
| Prioridad | Urgente |
| Tipo | Feature test / hardening harness |
| Track | `hardening_failure_testing_20260515` |
| Dueño sugerido | Nuevo agente executor con `opencode-go/qwen3.6-plus` |
| Commit | No commitear sin aprobación explícita |

## Qué debe probar

### Escenario principal: caos de stream

Simular una sesión donde ocurren todas estas cosas:

1. Kira tiene al menos 7 temas aprobados de agenda.
2. Cada tema pide respuesta extensa o expandida.
3. El chat genera ráfagas intensas con basura, spam, emojis, comentarios normales y comentarios “joyita”.
4. El streamer interrumpe varias veces con PTT mientras:
   - Kira habla;
   - Kira genera;
   - hay prefetch pendiente;
   - hay chat reactivo en cola;
   - la agenda está por cambiar de tema.
5. La cola se llena y fuerza overflow.
6. Algunas entradas de chat quedan viejas y deben expirar.

## Política esperada

| Fuente | Prioridad | Expira | Conducta esperada |
|---|---:|---|---|
| Streamer / PTT | 0 | No | Siempre gana sobre chat y agenda. No debe perderse por TTL. |
| Chat joyita | 1 | Sí, TTL corto | Puede interrumpir agenda si es reciente e interesante. No debe reaccionarse tarde. |
| Chat dominante | 1 | Sí, TTL corto | Se compacta y se responde solo si sigue siendo fresco. |
| Agenda | 2 | Sí | Rellena cuando no hay humano/chat importante. Debe ceder. |
| Chat basura | N/A | N/A | Debe filtrarse, compactarse o ignorarse. |

## Casos mínimos automatizados

### CT-001 — PTT gana bajo cola llena

**Dado** que hay agenda y chat encolados  
**Cuando** entra un PTT del streamer  
**Entonces** el próximo item procesado debe ser PTT.

### CT-002 — Chat viejo no produce reacción tardía

**Dado** que un chat reactivo quedó en cola más allá del TTL  
**Cuando** el motor vuelve a estar idle  
**Entonces** ese chat se omite y no se mueve a acumulación.

### CT-003 — Overflow preserva prioridad humana

**Dado** que la cola supera su tamaño máximo  
**Cuando** hay PTT, chat y agenda  
**Entonces** se descarta primero agenda, luego chat, nunca PTT mientras haya opciones de menor prioridad.

### CT-004 — Joyita vence texto largo aburrido

**Dado** un lote de mensajes recientes  
**Cuando** hay una pregunta, comentario gracioso, emoji fuerte o rareza válida  
**Entonces** `_select_highlight()` debe elegir esa joyita antes que un texto largo pero aburrido.

### CT-005 — 7 temas no repiten ni se traban

**Dado** 7 temas aprobados con respuestas extensas  
**Cuando** se simula avance de agenda con prefetch  
**Entonces** no debe haber repetición exacta/near-repeat, los contadores de turnos deben avanzar y la agenda debe terminar en estado válido.

### CT-006 — Interrupciones repetidas no dejan estado corrupto

**Dado** que Kira está hablando/generando/prefetcheando  
**Cuando** entran múltiples PTT del streamer  
**Entonces** se limpian prefetched stale, se preserva PTT y UI/avatar/audio-bed vuelven a estado válido.

### CT-007 — Chat hiperintenso no causa backlog viejo

**Dado** un generador de chat con perfil 2k viewers/noise burst  
**Cuando** el SmartAggregator activa live-safety/backpressure  
**Entonces** no debe crecer la cola sin límite, no deben dispararse LLM calls excesivas y Kira no debe responder mensajes viejos.

## Datos sintéticos requeridos

### Temas de agenda

Crear 7 temas con ángulos diferentes:

1. nostalgia tecnológica;
2. IA como co-host;
3. cultura de streaming;
4. videojuegos difíciles;
5. comunidades online;
6. privacidad local-first;
7. humor absurdo de chat.

### Chat basura

Incluir mensajes:

- spam repetido;
- emojis repetidos;
- texto muy corto;
- bait sin contenido;
- mayúsculas;
- duplicados por usuario;
- mensajes fuera de tema.

### Joyitas

Incluir mensajes como:

- `¿Kira acaba de descubrir el capitalismo con RGB?`
- `jajaja la IA se puso modo tía del cyber 😂`
- `si el modelo pesa 7b, ¿Kira cobra por kilo?`
- `esto suena a Windows 98 teniendo una crisis existencial`

### PTT streamer

Incluir interrupciones como:

- `Kira, pará un segundo y respondé esto.`
- `No sigas con ese tema, cambiemos el ángulo.`
- `Eso del chat está bueno, agarrá ese comentario.`
- `Cortá, volvamos al juego.`

## Métricas de aprobación

- PTT procesado antes que chat/agenda en todos los casos.
- 0 reacciones a chat expirado.
- 0 repeticiones exactas en agenda prefetched.
- 0 estados inválidos del `KiraAgendaController`.
- Cola prioritaria nunca supera el límite configurado.
- Accumulation buffer respeta límite de items/chars/TTL.
- El selector de joyitas elige al menos una joyita válida durante chat intenso.
- Tests no modifican `config/avatar.yaml` ni `config/music_library.json`.

## Archivos probables

| Archivo | Rol |
|---|---|
| `core/llm_engine.py` | Cola prioritaria, TTL, acumulación, playback/prefetch. |
| `smart_aggregator/kira_agenda_controller.py` | Estado de agenda, turnos, anti-loop, prefetch. |
| `ui/smart_aggregator_ui.py` | Selección de joyitas y envío de contexto. |
| `tests/test_llm_engine_timeouts.py` | Tests de cola/TTL/overflow. |
| `tests/test_kira_agenda_controller.py` | Tests de agenda larga/interrupciones. |
| `tests/test_smart_aggregator_ui.py` | Tests de highlight/chat context. |
| `tests/test_smart_aggregator.py` | Tests de carga/backpressure. |

## Restricciones para el agente executor

- No commitear.
- No tocar `config/music_library.json`.
- No escribir configs reales desde tests.
- Usar `python` para pytest.
- Preservar cambios no relacionados del working tree.
- Guardar descubrimientos importantes en Engram con project `voiceai`.
- Si una prueba requiere diseño nuevo, documentarlo antes de implementar.

## Prompt de handoff

Usar el prompt de `agent_handoff_prompt.md` en este mismo track.
