# RF4 — Escenarios de Prueba Esperados (Stream Admin Mode)

**Código:** RF4-TEST  
**Módulo:** Gestión de Stream / Admin Mode  
**Versión:** 0.1  
**Estado:** Diseño refinado  
**Fecha:** 2026-05-04

---

## 1. Escenarios Positivos

### TEST-RF4-001 — Conectar YouTube en modo solo lectura

**Precondiciones:**

- OpenCohost ejecutándose en `flux_env`.
- Credenciales OAuth configuradas.
- `stream_admin.yaml` con `provider.mode: read_only`.

**Pasos:**

1. Abrir pestaña `Stream Admin`.
2. Presionar `Conectar YouTube`.
3. Completar autorización OAuth.
4. Volver a OpenCohost.

**Resultado esperado:**

- La UI muestra proveedor conectado.
- Se muestran canal/cuenta y scopes activos.
- Botones de escritura aparecen deshabilitados.
- No se imprime ningún token en logs.

**Estado actual:** Pendiente.

---

### TEST-RF4-002 — Leer metadata del stream

**Precondiciones:**

- YouTube conectado en modo lectura.
- Canal con stream activo o metadata disponible.

**Pasos:**

1. Presionar `Actualizar metadata`.

**Resultado esperado:**

- La UI muestra título, categoría, descripción y tags actuales.
- Si una métrica no está disponible, se muestra `No disponible`.
- La acción se registra en `logs/acciones.jsonl` sin datos sensibles.

**Estado actual:** Pendiente.

---

### TEST-RF4-003 — Kira sugiere título con aprobación requerida

**Precondiciones:**

- `metadata.require_approval: true`.
- `metadata.allow_kira_suggestions: true`.
- RF3 enviando contexto de chat.

**Pasos:**

1. Simular cambio de tema en el stream.
2. Solicitar sugerencia a Kira.
3. Revisar propuesta en `Stream Admin`.

**Resultado esperado:**

- Kira propone título/categoría/tags.
- La propuesta queda pendiente, no se aplica automáticamente.
- El streamer puede editarla antes de aplicar.
- La propuesta queda registrada como `suggested`.

**Estado actual:** Pendiente.

---

### TEST-RF4-004 — Aplicar metadata tras aprobación

**Precondiciones:**

- OAuth con scopes de escritura.
- Propuesta pendiente aprobada por el streamer.

**Pasos:**

1. Editar título sugerido.
2. Presionar `Aplicar cambios`.

**Resultado esperado:**

- El proveedor recibe la actualización.
- La UI muestra metadata nueva.
- `Kira Acciones` registra cambio anterior y nuevo.

**Estado actual:** Pendiente.

---

### TEST-RF4-005 — Activar Slow Mode manualmente

**Precondiciones:**

- Proveedor conectado con permisos necesarios.
- Slow Mode soportado por proveedor.

**Pasos:**

1. Abrir `Stream Admin`.
2. Configurar Slow Mode a 10 segundos por 5 minutos.
3. Presionar `Activar Slow Mode`.

**Resultado esperado:**

- Slow Mode se activa.
- UI muestra estado activo.
- Acción queda registrada.
- Si `announce_actions_to_chat=true`, Kira anuncia la moderación.

**Estado actual:** Pendiente.

---

### TEST-RF4-006 — Auto Slow Mode por toxicidad con modo automático

**Precondiciones:**

- Moderación habilitada.
- `mode: automatic`.
- RF3 emite vibe negativo + chat velocity alta.

**Pasos:**

1. Simular ventana de chat con toxicidad alta.
2. Esperar evaluación RF4.

**Resultado esperado:**

- RF4 activa Slow Mode si supera umbrales.
- La acción respeta cooldown.
- La acción se registra con motivo y señales usadas.
- Kira solo anuncia si está configurado.

**Estado actual:** Pendiente.

---

### TEST-RF4-007 — Analíticas configurables inyectadas a Kira

**Precondiciones:**

- Analíticas habilitadas.
- `update_interval_seconds` configurado.
- LLM interface inyectada.

**Pasos:**

1. Simular viewers, chat velocity, vibe trend y uptime.
2. Esperar intervalo de actualización.

**Resultado esperado:**

- UI muestra métricas.
- Kira recibe contexto resumido, no mensajes repetitivos.
- Kira reacciona solo a hitos/eventos importantes configurados.

**Estado actual:** Pendiente.

---

### TEST-RF4-007B — Placeholder Twitch visible pero deshabilitado

**Precondiciones:**

- RF4 MVP instalado.
- YouTube configurado como proveedor principal.

**Pasos:**

1. Abrir pestaña `Stream Admin`.
2. Revisar selector de proveedor.

**Resultado esperado:**

- YouTube aparece disponible.
- Twitch aparece como `Próximamente` o `No disponible en MVP`.
- No se intenta autenticar Twitch.

**Estado actual:** Pendiente.

---

### TEST-RF4-007C — Kira envía mensaje al chat si está habilitado

**Precondiciones:**

- Proveedor conectado con permisos de escritura de chat.
- `announce_actions_to_chat: true`.
- Opción `Kira puede escribir al chat` habilitada en UI.

**Pasos:**

1. Ejecutar una acción anunciable, por ejemplo activar Slow Mode.

**Resultado esperado:**

- Kira deja un mensaje breve y natural en el chat.
- La acción también queda registrada en `Kira Acciones`.
- Si falta permiso de escritura, RF4 no crashea y muestra permiso insuficiente.

**Estado actual:** Pendiente.

---

## 2. Escenarios Negativos

### TEST-RF4-008 — Token expirado o revocado

**Precondiciones:**

- Token guardado inválido o revocado.

**Pasos:**

1. Iniciar OpenCohost.
2. Abrir `Stream Admin`.
3. Intentar leer metadata.

**Resultado esperado:**

- La UI muestra sesión expirada o permisos inválidos.
- Botones de escritura quedan deshabilitados.
- OpenCohost no crashea.
- No se borra configuración no relacionada.

**Estado actual:** Pendiente.

---

### TEST-RF4-009 — Intentar escribir en modo read-only

**Precondiciones:**

- `provider.mode: read_only`.

**Pasos:**

1. Intentar aplicar título/categoría.

**Resultado esperado:**

- El botón no está disponible o la acción se bloquea.
- Log: `RF4 escritura bloqueada: modo solo lectura`.
- No se realiza llamada de escritura al proveedor.

**Estado actual:** Pendiente.

---

### TEST-RF4-010 — API rate limit

**Precondiciones:**

- Proveedor responde con rate-limit.

**Pasos:**

1. Forzar varias actualizaciones seguidas.

**Resultado esperado:**

- RF4 registra aviso de rate-limit.
- Aplica backoff/cooldown.
- UI sigue funcionando.
- RF3/Core siguen funcionando.

**Estado actual:** Pendiente.

---

### TEST-RF4-011 — Acción no soportada por proveedor

**Precondiciones:**

- Proveedor conectado.
- Acción no disponible para ese proveedor.

**Pasos:**

1. Revisar controles de moderación.

**Resultado esperado:**

- La acción aparece como `No soportada` o no se muestra.
- No hay error fatal.
- El log explica la limitación.

**Estado actual:** Pendiente.

---

## 3. Escenarios de Seguridad

### TEST-RF4-012 — Logs sin secretos

**Precondiciones:**

- OAuth completado.
- Acciones de lectura/escritura ejecutadas.

**Pasos:**

1. Revisar logs de consola y `logs/acciones.jsonl`.

**Resultado esperado:**

- No aparecen access tokens.
- No aparecen refresh tokens.
- No aparecen client secrets.
- Solo se registran datos operativos necesarios.

**Estado actual:** Pendiente.

---

### TEST-RF4-013 — Acción de alto riesgo requiere confirmación

**Precondiciones:**

- Moderación automática activa.
- Acción detectada: timeout/ban.

**Pasos:**

1. Simular usuario tóxico.
2. Esperar recomendación de RF4.

**Resultado esperado:**

- RF4 propone timeout/ban.
- No ejecuta sin confirmación por defecto, aunque timeout/ban estén incluidos en MVP.
- El streamer puede aprobar o rechazar.

**Estado actual:** Pendiente.

---

## 4. Matriz de Cobertura

| Escenario | RF4.1 | RF4.2 | RF4.3 | RF4.4 | RNF4.1 | RNF4.2 | RNF4.3 | RNF4.4 |
|-----------|-------|-------|-------|-------|--------|--------|--------|--------|
| TEST-RF4-001 | Sí | No | No | Sí | Sí | Sí | Sí | No |
| TEST-RF4-002 | Sí | No | No | Sí | Sí | No | Sí | Sí |
| TEST-RF4-003 | Sí | No | No | Sí | No | Sí | No | No |
| TEST-RF4-004 | Sí | No | No | Sí | Sí | Sí | Sí | Sí |
| TEST-RF4-005 | No | Sí | No | Sí | Sí | Sí | Sí | Sí |
| TEST-RF4-006 | No | Sí | Sí | Sí | Sí | Sí | Sí | Sí |
| TEST-RF4-007 | No | No | Sí | Sí | No | No | Sí | Sí |
| TEST-RF4-008 | Sí | Sí | Sí | Sí | Sí | Sí | Sí | No |
| TEST-RF4-009 | Sí | No | No | Sí | Sí | Sí | Sí | No |
| TEST-RF4-010 | Sí | Sí | Sí | Sí | No | No | Sí | Sí |
| TEST-RF4-011 | Sí | Sí | No | Sí | No | Sí | Sí | No |
| TEST-RF4-012 | Sí | Sí | Sí | Sí | Sí | No | No | No |
| TEST-RF4-013 | No | Sí | No | Sí | No | Sí | No | No |
