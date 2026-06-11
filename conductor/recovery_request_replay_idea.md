# Idea de Implementación: Replay de Peticiones tras Recuperación (Request Replay)

**Estado**: Pendiente / Idea para SDD proposal futuro (Sin prioridad)
**Origen**: Validación runtime de Gate 1 — `runtime_validation_gates_20260610` (2026-06-11)

## Problema

Cuando el watchdog de inferencia detecta un modelo colgado, la recuperación funciona
(cancelación, rollback al último modelo bueno, liberación de memoria), pero la
petición del usuario **se descarta silenciosamente**: `core/llm_engine.py` retorna
`""` tras `_recover_from_stalled_inference()` y no existe reintento contra el modelo
de fallback ni mecanismo en la UI para reenviar el mensaje perdido.

Evidencia: sesión del 2026-06-11 (`logs/voiceai_20260611_084746.log`) — Gate 1 PASS,
pero el mensaje enviado a `gemma:26b` se perdió tras el rollback a `gemma4:e4b`.

El registro de fallo existente (`_last_llm_failure`) guarda modelo, intento y razón,
pero NO los `messages` — exactamente lo que se necesitaría para reproducir la petición.

## Diseño Propuesto (a decidir en el proposal)

### 1. Captura del sobre de petición (Request Envelope)
Extender el registro de fallo para preservar la petición completa como dato:
modelo solicitado, `messages`, opciones de generación, fuente (`direct`/agenda),
y timestamp. Sin esto, ninguna estrategia de replay es posible.

### 2. Estrategia de replay — tres opciones con tradeoffs UX

- **A. Auto-replay en el modelo de fallback**: reintento automático tras el rollback.
  Riesgo: la recuperación completa toma ~90s (45s ventana + ~45s recarga); en un
  stream en vivo, responder una pregunta de hace dos minutos puede ser peor que
  perderla. Riesgo adicional: re-cuelgue si la petición misma es el problema.
- **B. Afordancia en UI**: notificación "tu mensaje no se procesó" con botón de
  reintento manual. Probablemente la opción correcta para un cohost supervisado;
  requiere trabajo de UI nuevo.
- **C. Drop con notificación clara**: comportamiento actual mejorado (mensaje
  visible en UI indicando qué se perdió). La opción barata.

### 3. Idempotencia como prerrequisito del replay
Para que cualquier reintento sea seguro, debe garantizarse que re-ejecutar no
duplique efectos: que no se sintetice/reproduzca audio dos veces, que no se
escriban entradas duplicadas en memoria de conversación ni en `acciones.jsonl`.
Se conecta con el ID de correlación propuesto en `api_llm_provider_idea.md`
(sección "Trazabilidad e Idempotencia") — ambas ideas comparten ese cimiento.

## Fuera de Alcance (Out of Scope)

- No implementar ahora: el modo operativo vigente es validación controlada, no
  expansión. La recuperación actual funciona como mecanismo de seguridad; perder
  un mensaje en un fallo poco frecuente es aceptable para el launch de OpenCohost.
- El path de errores de transporte (mismo patrón de `return ""`) puede sumarse al
  alcance del proposal cuando se escriba.
