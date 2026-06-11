# RF4 — Requerimientos Funcionales: Stream Admin Mode

**Código:** RF4  
**Módulo:** Gestión de Stream / Admin Mode  
**Versión:** 0.1  
**Estado:** MVP base implementado  
**Fecha:** 2026-05-04

---

## 1. Alcance

Este documento define los requerimientos funcionales para el módulo **Stream Admin Mode** de OpenCohost. Su propósito es permitir que Kira ayude al streamer con tareas administrativas del stream: lectura y modificación segura de metadata, moderación asistida/automática y uso de analíticas en tiempo real como contexto conversacional.

RF4 debe priorizar la seguridad del usuario. La primera etapa debe operar en **modo solo lectura** para validar OAuth, permisos, tokens, lectura de datos y UI antes de permitir acciones que modifiquen el stream.

---

## 2. Principios de Diseño

1. **YouTube primero, Twitch compatible a futuro:** La implementación inicial debe priorizar YouTube porque RF3 ya consume YouTube Live Chat. La arquitectura no debe cerrar la puerta a Twitch.
2. **Seguridad antes que automatización:** OAuth 2.0 debe implementarse con scopes mínimos, almacenamiento seguro de tokens y revocación clara.
3. **Aprobación por defecto:** Cambios de título, categoría, descripción o tags requieren aprobación del streamer salvo que una configuración explícita habilite auto-aprobación.
4. **Streamer siempre puede intervenir:** Cualquier sugerencia de Kira debe ser editable antes de ejecutarse.
5. **Automatización configurable:** Moderación, anuncios y reacciones por analíticas deben poder activarse/desactivarse desde UI.
6. **Módulo separado con contratos:** RF4 debe vivir como módulo independiente recomendado `stream_admin/`, consumiendo eventos de RF3 por callbacks o payloads, sin modificar `core/llm_engine.py`.
7. **Un solo LLM:** RF4 no debe cargar otro modelo. Si necesita razonamiento o sugerencias, debe usar una interfaz LLM inyectada, igual que RF3.

---

## 3. Requerimientos Funcionales

### RF4.1 — Integración YouTube/Twitch API para Metadata

| Campo | Descripción |
|-------|-------------|
| **ID** | RF4.1 |
| **Nombre** | Gestión segura de metadata del stream |
| **Prioridad** | Alta |
| **Actor** | Streamer |
| **Proveedor inicial** | YouTube |
| **Proveedor futuro** | Twitch |

**Descripción:**  
El sistema debe permitir leer y, cuando el streamer lo autorice, modificar metadata del stream: título, categoría/juego, descripción, tags/etiquetas y presets definidos por el streamer.

**Sub-requerimientos:**

- **RF4.1a — Provider abstraction:** Crear una interfaz común para proveedores (`YouTubeProvider`, `TwitchProvider` futuro) con métodos de lectura y escritura de metadata.
- **RF4.1b — Modo solo lectura inicial:** La primera versión operativa debe autenticar y leer datos del canal/stream sin ejecutar cambios.
- **RF4.1c — OAuth 2.0 seguro:** Implementar OAuth con scopes mínimos. No usar API keys para acciones privadas porque no permiten modificar metadata.
- **RF4.1d — Lectura de metadata:** Mostrar en UI título actual, categoría actual, descripción, tags y estado de stream cuando el proveedor lo permita.
- **RF4.1e — Edición manual:** El streamer puede editar título, categoría, descripción y tags desde la UI.
- **RF4.1f — Presets del streamer:** El streamer puede guardar presets de títulos y categorías frecuentes.
- **RF4.1g — Categorías predeterminadas:** El streamer puede definir una lista de categorías/juegos permitidos para cambios rápidos.
- **RF4.1h — Sugerencias de Kira:** Kira puede sugerir título, categoría, descripción o tags basándose en el contexto del stream y RF3.
- **RF4.1i — Aprobación por defecto:** Toda sugerencia de Kira debe quedar pendiente de aprobación antes de ejecutarse.
- **RF4.1j — Auto-aprobación opcional:** El streamer puede habilitar explícitamente que Kira aplique cambios sin aprobación previa.
- **RF4.1k — Edición antes de aplicar:** Aunque Kira sugiera un cambio, el streamer puede modificarlo manualmente antes de ejecutarlo.
- **RF4.1l — Auditoría:** Toda lectura, sugerencia, aprobación, rechazo y cambio aplicado debe registrarse en `logs/acciones.jsonl`.

**Comportamiento esperado:**

- Default: RF4 puede leer metadata, pero no modificarla.
- Si Kira sugiere un título, aparece como propuesta editable en la pestaña `Stream Admin`.
- Si `auto_apply_metadata=false`, el botón `Aplicar` requiere acción manual del streamer.
- Si `auto_apply_metadata=true`, Kira puede aplicar cambios, pero debe registrar el motivo, el payload anterior y el payload nuevo.
- Si el proveedor falla o revoca permisos, la UI debe pasar a modo seguro: lectura/escritura deshabilitada y mensaje claro en logs.

**Reglas de negocio:**

1. RF4 no debe guardar secretos en texto plano sin protección.
2. RF4 no debe solicitar scopes de escritura hasta que el usuario active funciones de escritura.
3. RF4 no debe aplicar cambios destructivos sin log de auditoría.
4. Si existe duda sobre el proveedor, categoría o permisos, no ejecutar cambios.
5. Twitch debe diseñarse como implementación futura, no como bloqueo para YouTube.

---

### RF4.2 — Moderación Automática y Asistida

| Campo | Descripción |
|-------|-------------|
| **ID** | RF4.2 |
| **Nombre** | Moderación asistida/automática del chat |
| **Prioridad** | Alta |
| **Actor** | Streamer, Kira, Chat |

**Descripción:**  
El sistema debe usar señales de RF3, listas configurables y datos del proveedor para detectar situaciones de riesgo en el chat y ejecutar acciones de moderación manuales, semi-automáticas o automáticas según configuración del streamer.

**Sub-requerimientos:**

- **RF4.2a — Modos de autonomía:** La UI debe ofrecer tres modos: `Solo alertas`, `Confirmación requerida` y `Automático`.
- **RF4.2b — Alerta por toxicidad:** Si RF3 detecta sentimiento negativo masivo, Kira debe mandar un mensaje al log de acciones de Kira. No debe actuar automáticamente si el modo no lo permite.
- **RF4.2c — Slow Mode configurable:** El streamer puede activar/desactivar Slow Mode manualmente y configurar duración/intensidad.
- **RF4.2d — Auto Slow Mode:** Si el modo automático está activo y se supera el umbral configurado, Kira puede activar Slow Mode por N minutos.
- **RF4.2e — Emote-Only / Followers-Only / Subscribers-Only:** Deben exponerse solo si el proveedor los soporta. Si no están disponibles, la UI debe mostrarlos como no soportados.
- **RF4.2f — Timeout/Ban:** Acciones contra usuarios específicos se consideran de alto riesgo y deben requerir confirmación por defecto, incluso si otros modos son automáticos.
- **RF4.2g — Lista negra configurable:** El streamer puede definir términos prohibidos, severidad y acción sugerida.
- **RF4.2h — Señales combinadas:** Los triggers deben balancear vibe negativo, velocidad de chat, palabras prohibidas y repetición/spam.
- **RF4.2i — Anuncio opcional al chat:** Kira puede anunciar una acción de moderación en el chat si el streamer lo configura.
- **RF4.2j — Auditoría de moderación:** Toda alerta, recomendación, acción aplicada y rollback debe registrarse.

**Comportamiento esperado:**

- `Solo alertas`: RF4 escribe en `Kira Acciones`, no toca el stream.
- `Confirmación requerida`: RF4 propone acción y espera confirmación en UI.
- `Automático`: RF4 ejecuta acciones permitidas por configuración, con cooldown y auditoría.
- Anunciar acciones al chat es opcional y desactivado por defecto.
- Acciones de alto riesgo (`timeout`, `ban`) requieren confirmación salvo configuración avanzada explícita.

**Reglas de negocio:**

1. El streamer debe poder desactivar toda moderación automática con un switch maestro.
2. Cada acción automática debe tener cooldown para evitar toggles repetidos.
3. La moderación debe ser reversible cuando el proveedor lo permita.
4. La lista negra debe poder editarse sin reiniciar la app.
5. Las acciones deben registrar proveedor, canal, timestamp, criterio y resultado.

---

### RF4.3 — Analíticas e Inyección de Contexto

| Campo | Descripción |
|-------|-------------|
| **ID** | RF4.3 |
| **Nombre** | Analíticas de stream para contexto de Kira |
| **Prioridad** | Media-Alta |
| **Actor** | Kira, Streamer |

**Descripción:**  
El sistema debe leer analíticas disponibles del proveedor y de RF3 para mostrarlas en UI e inyectarlas en el contexto de Kira de forma controlada, configurable y sin saturar al LLM.

**Datos requeridos:**

- Viewers concurrentes.
- Eventos de suscripción/follow/donación/bits cuando el proveedor lo soporte.
- Chat velocity / mensajes por segundo.
- Vibe trend del chat.
- Stream uptime.
- Hitos configurables: viewers máximos, nuevos subs, rachas de actividad, cambios fuertes de vibe.

**Sub-requerimientos:**

- **RF4.3a — Panel de analíticas:** La pestaña `Stream Admin` debe mostrar métricas actuales y estado de conexión.
- **RF4.3b — Configuración desde UI:** El streamer puede activar/desactivar qué métricas se recolectan, muestran e inyectan.
- **RF4.3c — Frecuencia configurable:** El intervalo de actualización debe ser configurable.
- **RF4.3d — Inyección controlada:** Las analíticas deben inyectarse como contexto resumido, no como mensajes continuos.
- **RF4.3e — Reacciones MIX:** Kira puede reaccionar a hitos y eventos importantes, pero tendencias generales deben preferirse como contexto silencioso.
- **RF4.3f — Cooldown de reacciones:** Hitos repetidos deben tener cooldown para evitar que Kira interrumpa demasiado.
- **RF4.3g — Persistencia:** Métricas y eventos relevantes deben poder guardarse para resumen posterior.

**Comportamiento esperado:**

- Default: mostrar analíticas disponibles, inyectar solo resumen cada intervalo configurado.
- Kira puede reaccionar a subs/donaciones/hitos si el streamer lo habilita.
- Kira no debe narrar métricas cada minuto si no ocurrió algo relevante.
- Si una métrica no está soportada por el proveedor, debe aparecer como `No disponible`, no como error.

---

### RF4.4 — UI Stream Admin y Registro de Acciones

| Campo | Descripción |
|-------|-------------|
| **ID** | RF4.4 |
| **Nombre** | Pestaña Stream Admin |
| **Prioridad** | Alta |
| **Actor** | Streamer |

**Descripción:**  
La UI debe incluir una nueva pestaña `Stream Admin` para gestionar autenticación, metadata, moderación y analíticas sin mezclar controles administrativos con el chat normal.

**Sub-requerimientos:**

- **RF4.4a — Nueva pestaña:** Agregar `Stream Admin` al Tabview principal.
- **RF4.4b — Estado OAuth:** Mostrar proveedor conectado, cuenta/canal, scopes activos y modo read-only/write.
- **RF4.4c — Metadata:** Campos editables para título, categoría, descripción, tags y presets.
- **RF4.4d — Moderación:** Switch maestro, modo de autonomía, botones manuales y configuración de umbrales.
- **RF4.4e — Analíticas:** Métricas visibles y controles de frecuencia/activación.
- **RF4.4f — Acciones pendientes:** Mostrar sugerencias de Kira pendientes de aprobación, edición o rechazo.
- **RF4.4g — Log dedicado:** Las acciones deben seguir registrándose en `Kira Acciones` y persistir en `logs/acciones.jsonl`.

---

## 4. Arquitectura Recomendada

Se recomienda crear un módulo nuevo:

```text
stream_admin/
├── __init__.py
├── admin_manager.py          # Orquestador RF4
├── providers.py              # Interfaces comunes
├── youtube_provider.py       # OAuth + YouTube Data API
├── twitch_provider.py        # Futuro OAuth + Twitch API
├── moderation.py             # Reglas/modos de moderación
├── analytics.py              # Métricas y contexto
├── oauth_store.py            # Manejo seguro de tokens
└── test_local.py             # Tests headless RF4
```

**Relación con RF3:**  
RF4 debe ser separado, pero puede consumir señales de RF3 mediante callbacks/eventos:

- `vibe_result`
- `activity_trigger`
- `chat_rate`
- `filtered_message_stats`
- `session_id`

**Relación con core:**  
RF4 no debe modificar `core/llm_engine.py`. Si requiere LLM, debe recibir `llm_interface` inyectada desde UI, igual que RF3.

---

## 5. Configuración Esperada

Archivo recomendado: `config/stream_admin.yaml`

```yaml
provider:
  default: youtube
  enabled_providers:
    - youtube
  mode: read_only

oauth:
  token_storage: local_file_mvp
  token_path: data/stream_admin/oauth_tokens.json
  request_write_scopes: false
  allow_token_export: false

metadata:
  require_approval: true
  auto_apply_metadata: false
  allow_kira_suggestions: true
  presets_enabled: true

moderation:
  enabled: false
  mode: alerts_only
  announce_actions_to_chat: false
  slow_mode:
    enabled: true
    default_seconds: 10
    duration_minutes: 5
  high_risk_actions_require_confirmation: true

analytics:
  enabled: true
  update_interval_seconds: 60
  inject_context: true
  react_to_milestones: true
  react_to_subs_donations: true
```

---

## 6. Respuestas del Usuario Registradas

| Tema | Respuesta |
|------|-----------|
| Plataforma inicial | YouTube primero, sin negar Twitch futuro |
| Metadata | Todo: título, categoría, descripción, tags/etiquetas |
| Cambios de título | Requieren aprobación salvo configuración contraria |
| Sugerencias de Kira | Sí, editable antes de aplicar |
| Categorías | Predeterminadas por streamer + sugeridas por Kira |
| Presets | Sí |
| OAuth | Priorizar seguridad; primero solo lectura |
| Almacenamiento tokens MVP | Lo más sencillo para MVP: archivo local protegido, fuera de git, sin tokens en logs; migrar a keyring/cifrado después |
| Moderación | Tres modos: alertas, confirmación, automático |
| Acciones moderación | Todo, balanceado y configurable |
| Toxicidad RF4.2.1 | Solo mensaje a logs de Kira |
| Auto Slow Mode RF4.2.2 | Sí, si streamer puede activar/desactivar |
| Anuncio RF4.2.5 | Alternativa valiosa, solo si configurado |
| Mensajes al chat | Kira puede dejar mensajes en el chat si el streamer lo habilita |
| Timeout/Ban | Entran en MVP, tratados como acciones de alto riesgo con confirmación por defecto |
| Analíticas RF4.3 | Todos los datos, configurables desde UI |
| Reacciones de Kira | Mix: hitos/eventos sí, tendencias como contexto |
| Frecuencia analíticas | Configurable |
| Arquitectura | Valorar separado vs dependiente; recomendado separado con contrato RF3 |
| Restricciones RF3 | Sí, mantener mismas restricciones |
| UI | Nueva pestaña |
| Twitch | Placeholder desde MVP, implementación real futura |
| Presets | En YAML para MVP |

---

## 7. Matriz de Trazabilidad

| RF | Componente | Archivos esperados |
|----|------------|-------------------|
| RF4.1 | Metadata/API/OAuth | `stream_admin/admin_manager.py`, `stream_admin/youtube_provider.py`, `stream_admin/oauth_store.py`, `config/stream_admin.yaml` |
| RF4.2 | Moderación | `stream_admin/moderation.py`, `stream_admin/admin_manager.py`, `ui/app.py` |
| RF4.3 | Analíticas | `stream_admin/analytics.py`, `stream_admin/admin_manager.py`, `ui/app.py` |
| RF4.4 | UI/Admin Log | `ui/app.py`, `logs/acciones.jsonl` |

---

## 8. Decisiones MVP Finales

1. **Tokens OAuth:** Para MVP se usará la opción más simple: archivo local en `data/stream_admin/oauth_tokens.json`, ignorado por git, sin tokens en logs y con permisos locales restrictivos cuando sea posible. Migración futura recomendada: Windows Credential Manager/keyring o cifrado.
2. **Proveedor:** MVP con YouTube real en modo read-only inicial y placeholder visible para Twitch deshabilitado.
3. **Mensajes de Kira al chat:** Permitidos si el streamer habilita la opción. Default: desactivado hasta configurar permisos.
4. **Timeout/Ban:** Entran en MVP, pero como acciones de alto riesgo. Default: requieren confirmación incluso si el modo general está en automático.
5. **Presets:** Títulos, categorías, descripciones base y tags viven en `config/stream_admin.yaml` durante MVP.

---

## 9. Estado de Implementación MVP — 2026-05-04

- [x] Módulo `stream_admin/` creado.
- [x] `AdminManager` implementado.
- [x] Provider YouTube con OAuth/API preparado usando `requests` y stdlib.
- [x] Placeholder Twitch implementado.
- [x] Token store local MVP en `data/stream_admin/oauth_tokens.json` ignorado por git.
- [x] Configuración `config/stream_admin.yaml` creada.
- [x] Pestaña `Stream Admin` integrada en `ui/app.py`.
- [x] Credenciales OAuth YouTube configurables desde UI y guardadas localmente fuera de git.
- [x] Metadata editable desde UI.
- [x] Sugerencias de Kira con aprobación por defecto.
- [x] Mensajes de Kira al chat condicionados a permiso/configuración.
- [x] Timeout/Ban entran como acciones de alto riesgo con confirmación.
- [x] Analíticas RF3 se consumen e inyectan como contexto silencioso.
- [x] Tests locales RF4 agregados.
- [x] Operación para streams chicos: `Stream Chico`, `Simular Chat` y `Forzar Kira` desde UI.
- [x] Lista de usuarios recientes con razón editable y botones `Timeout` / `Banear` desde `Stream Admin`.
- [x] OAuth YouTube probado con credenciales reales del usuario.
- [x] Escritura real de metadata y chat validada en canal real.
- [ ] Moderación `Timeout`/`Banear` validada con un usuario real distinto al owner.

---

## 10. Cierre Funcional RF4 MVP — 2026-05-05

RF4 queda cerrado como MVP funcional para YouTube Stream Admin. El módulo puede operar sobre lives privados/no listados usando OAuth, administrar metadata, conectar chat autenticado, enviar mensajes al chat, alimentar RF3 con mensajes reales, forzar respuestas de Kira para streams pequeños y preparar acciones de moderación por usuario.

Queda fuera del cierre MVP:

- Twitch real.
- Slow Mode YouTube, porque la API disponible en este MVP no expone una acción directa equivalente.
- Emote-only / Followers-only.
- Almacenamiento de tokens en Windows Credential Manager/keyring; el MVP usa archivo local ignorado por git.
- Validación real de `Timeout`/`Banear` contra un usuario no-owner.
