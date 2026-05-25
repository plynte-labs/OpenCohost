# Startup and Model Lifecycle Polish

## Overview

VoiceAI ya demostró buena resiliencia en stream hostil real, pero el arranque y cierre de subsistemas todavía puede sentirse ruidoso o poco profesional para un usuario normal. Este track pule el ciclo de vida de Ollama, OBS y los procesos/modelos administrados por VoiceAI para que la app diferencie estados transitorios de fallos reales, cierre recursos propios limpiamente y no deje memoria/VRAM secuestrada al salir.

La prioridad no es cambiar cómo Kira responde, genera audio o filtra chat. La prioridad es que el usuario entienda qué está pasando al iniciar/cerrar y que los recursos levantados por VoiceAI se liberen de forma segura.

## Goals

- Distinguir estados de startup: `starting`, `waiting`, `degraded`, `ready`, `failed`.
- Reducir ruido de errores prematuros durante arranque normal de Ollama/OBS.
- Mostrar/loguear mensajes más honestos: "iniciando", "esperando", "degradado" o "falló" según corresponda.
- Liberar procesos/modelos propios al cerrar VoiceAI normalmente.
- Evitar que modelos o subprocesses iniciados por VoiceAI queden consumiendo RAM/VRAM después de cerrar la app.
- Detectar y manejar procesos huérfanos o restos temporales en el siguiente inicio.
- Mantener graceful degradation: si Ollama/OBS no están listos, la app no debe colapsar.

## Non-Goals

- No cambiar prompts, personalidad, memoria conversacional o comportamiento de Kira.
- No cambiar SmartAggregator, filtros de chat, RF3, Agenda ni analytics.
- No cambiar calidad TTS, fragmentación TTS ni fallback policy salvo diagnósticos de lifecycle.
- No matar procesos externos que el usuario abrió fuera de VoiceAI.
- No implementar dashboard, métricas post-stream ni exporter de analytics.
- No reestructurar UI grande ni migrar tecnologías.

## Functional Requirements

### Startup state model

- VoiceAI debe representar Ollama y OBS con estados explícitos y no binarios.
- Un fallo durante la ventana inicial de arranque debe ser tratado como estado transitorio, no como error crítico inmediato.
- Después de agotar retries/backoff configurados, el estado puede pasar a `degraded` o `failed` con mensaje accionable.
- Los logs deben permitir distinguir:
  - servicio todavía iniciando.
  - servicio externo no disponible.
  - proceso iniciado por VoiceAI que falló.
  - servicio listo.

### Ollama lifecycle

- Si VoiceAI inicia o administra un proceso/modelo, debe registrar ownership explícito.
- Al cierre normal, VoiceAI debe intentar liberar/cerrar solo recursos propios.
- Si Ollama ya estaba abierto por fuera, VoiceAI no debe matarlo brutalmente.
- Si el modelo queda cargado pero el proceso pertenece a VoiceAI, el shutdown debe intentar descargar/liberar memoria con timeout corto.
- Si el cierre limpio falla, VoiceAI debe registrar advertencia y no colgar indefinidamente la UI.

### Qwen/TTS server lifecycle

- Si VoiceAI levanta el servidor Qwen/TTS pesado, debe poder apagarlo limpiamente durante el cierre normal.
- La limpieza debe incluir archivos temporales generados por VoiceAI cuando corresponda.
- En crash o cierre forzado, el siguiente inicio debe ejecutar una recuperación/janitor segura para restos obvios.
- El janitor no debe borrar archivos de usuario ni assets reales.

### OBS startup polish

- La conexión OBS debe diferenciar "OBS todavía no está listo" de "credenciales/configuración inválida".
- Reintentos iniciales no deben producir spam de modales ni warnings fuertes.
- El operador debe recibir aviso solo cuando el estado sea relevante y accionable.

### UI/operator feedback

- La UI puede mostrar estados de startup/degraded si ya existe un lugar seguro para hacerlo.
- Si no hay una integración UI de bajo riesgo, se permite limitar el MVP a logs claros y notificaciones no bloqueantes ya existentes.
- No se deben introducir modales bloqueantes nuevos.

## Safety Boundaries

Archivos permitidos esperados:

- `core/health_monitor.py`
- `core/llm_engine.py` solo si es necesario para lifecycle/diagnóstico de Ollama.
- `server_qwen.py` solo si es necesario para shutdown/health del servidor.
- `core/temp_file_cleanup.py` para janitor/lifecycle cleanup.
- `ui/app_shell.py` solo para orquestación de startup/shutdown y notificación no bloqueante.
- tests relacionados: `tests/test_health_monitor.py`, `tests/test_app_shell_obs_resilience.py`, `tests/test_llm_engine_timeouts.py`, `tests/test_temp_file_cleanup.py` o nuevos tests específicos.

Archivos que NO deben tocarse salvo aprobación explícita:

- `smart_aggregator/*`
- prompts/perfiles/personality config.
- lógica de generación principal de Kira.
- LiveVoice continuous/PTT.
- assets de avatar.
- analytics/exporter/spec futuro.

## Non-Functional Requirements

- Shutdown no debe bloquear la UI indefinidamente.
- Todos los cierres deben tener timeouts razonables y logs claros.
- Operaciones de cleanup deben ser idempotentes.
- Thread safety obligatoria para estados compartidos.
- No se debe introducir raw chat logging ni persistencia nueva de datos de usuario.
- Debe mantenerse compatibilidad con usuarios que corren Ollama manualmente.

## Acceptance Criteria

- Durante arranque normal lento, Ollama/OBS no generan falso error crítico inmediato.
- Después de retries agotados, el usuario ve/loguea un estado degradado o fallido accionable.
- Al cerrar VoiceAI, los procesos/modelos iniciados por VoiceAI se intentan cerrar/liberar.
- VoiceAI no mata procesos externos no propios.
- Si hubo crash anterior, el siguiente inicio puede limpiar restos seguros y reportar recuperación.
- Tests cubren ownership de proceso, shutdown con timeout, proceso externo protegido, retries startup y janitor idempotente.
- No hay regresiones en salud Qwen/Ollama/OBS ni en tests existentes de resiliencia.

## Risks

- Matar un proceso externo del usuario por error sería grave. Ownership debe ser explícito y testeado.
- Cleanup agresivo puede borrar archivos válidos. El janitor debe limitarse a paths/temp files controlados por VoiceAI.
- Shutdown sin timeout puede colgar la app. Todo cierre debe ser acotado.
- Cambios en health monitor pueden alterar fallback behavior. Mantener cambios en diagnóstico/estado, no en política de generación.

## Open Questions Before Implementation

- Confirmar si el primer MVP mostrará estados en UI o solo logs/notificaciones no bloqueantes.
- Confirmar comandos/API disponibles para liberar modelo en Ollama sin cerrar un Ollama externo.
- Confirmar cómo se identifica de forma robusta un proceso Qwen/TTS iniciado por VoiceAI en Windows.
