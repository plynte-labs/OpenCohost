# Handoff — Preparación RF4 Stream Admin Mode

**Fecha:** 2026-05-04  
**Módulo:** Gestión de Stream / Admin Mode  
**Estado:** MVP base implementado, pendiente validación OAuth real  
**Ambiente de ejecución esperado:** `E:\Miniconda\envs\flux_env\python.exe main.py`

---

## 1. Contexto Actual

VoiceAI ya tiene RF3 Smart Aggregator integrado para YouTube Live Chat. RF4 debe construirse encima del contexto operativo de RF3, pero sin mezclarse con su código interno.

RF4 debe encargarse de tareas administrativas del stream:

- Leer y modificar metadata del stream.
- Sugerir cambios de título/categoría/descripcion/tags.
- Moderar el chat de forma asistida o automática.
- Leer analíticas y usarlas como contexto para Kira.

La prioridad inicial es seguridad: **primero solo lectura** con OAuth 2.0 seguro.

---

## 2. Documentos Creados

| Archivo | Contenido |
|---------|-----------|
| `docs/RF4_Functional_Requirements.md` | Alcance, RF4.1-RF4.4, arquitectura, configuración, respuestas del usuario y dudas pendientes. |
| `docs/RF4_Quality_Requirements.md` | Seguridad OAuth, control del streamer, robustez API, rate limits, privacidad y criterios de aceptación. |
| `docs/RF4_Test_Scenarios.md` | Escenarios positivos, negativos, seguridad y matriz de cobertura. |
| `docs/HANDOFF_RF4.md` | Guía de implementación y decisiones registradas. |

---

## 3. Decisiones Registradas

1. **Proveedor inicial:** YouTube primero.
2. **Twitch:** Debe quedar soportable por arquitectura, pero no bloquea MVP.
3. **Metadata:** Título, categoría, descripción, tags y presets.
4. **Aprobación:** Cambios requieren aprobación salvo configuración explícita.
5. **Kira:** Puede sugerir metadata y el streamer puede editar antes de aplicar.
6. **Categorías:** Predeterminadas por streamer + sugeridas por Kira.
7. **OAuth:** Priorizar seguridad; primera etapa solo lectura.
8. **Moderación:** Tres modos: `Solo alertas`, `Confirmación requerida`, `Automático`.
9. **RF4.2.1:** Toxicidad masiva solo manda mensaje al log de Kira.
10. **RF4.2.2:** Auto Slow Mode permitido si el streamer lo habilita.
11. **RF4.2.5:** Kira puede anunciar moderación al chat si está configurado.
12. **Analíticas:** Todas las propuestas, configurables desde UI.
13. **Reacciones:** Mix: hitos/eventos sí; tendencias como contexto silencioso.
14. **Frecuencia:** Configurable.
15. **UI:** Nueva pestaña `Stream Admin`.
16. **Restricciones:** Mismas restricciones RF3: un solo LLM, YAML, callbacks, no modificar core.

---

## 4. Arquitectura Recomendada

Crear módulo separado:

```text
stream_admin/
├── __init__.py
├── admin_manager.py
├── providers.py
├── youtube_provider.py
├── twitch_provider.py
├── moderation.py
├── analytics.py
├── oauth_store.py
└── test_local.py
```

**Razón:** RF4 tiene responsabilidades distintas a RF3. RF3 lee y agrega chat; RF4 administra el stream y maneja OAuth, escritura y moderación. Mezclarlos aumentaría riesgo y complejidad.

**Contrato con RF3:** RF4 puede recibir eventos desde RF3:

- Vibe result.
- Activity trigger.
- Chat velocity.
- Estadísticas de mensajes filtrados.
- Session id.

**Contrato con UI:** UI instancia `AdminManager`, registra callbacks y muestra pestaña `Stream Admin`.

**Contrato con LLM:** RF4 recibe `llm_interface` inyectada si necesita sugerencias de Kira.

---

## 5. Plan de Implementación Tentativo

| Paso | RF | Cambio | Riesgo |
|------|----|--------|--------|
| 1 | RF4.1 | Crear `stream_admin/` con interfaces y manager headless | Bajo |
| 2 | RF4.1 | Crear `config/stream_admin.yaml` con defaults seguros | Bajo |
| 3 | RF4.1 | Implementar OAuth read-only YouTube | Alto |
| 4 | RF4.4 | Agregar pestaña `Stream Admin` con estado OAuth y metadata read-only | Medio |
| 5 | RF4.1 | Agregar presets y propuestas editables sin escritura real | Medio |
| 6 | RF4.3 | Agregar analíticas configurables e inyección de contexto | Medio |
| 7 | RF4.2 | Agregar motor de moderación local/simulado con logs | Medio |
| 8 | RF4.1/RF4.2 | Habilitar scopes de escritura y acciones reales tras aprobación | Alto |
| 9 | RF4.2 | Agregar anuncios opcionales al chat si el proveedor lo permite | Alto |
| 10 | RF4 futuro | Implementar provider Twitch | Medio-Alto |

---

## 6. Archivos Esperados a Modificar o Crear

### Crear

- `stream_admin/__init__.py`
- `stream_admin/admin_manager.py`
- `stream_admin/providers.py`
- `stream_admin/youtube_provider.py`
- `stream_admin/moderation.py`
- `stream_admin/analytics.py`
- `stream_admin/oauth_store.py`
- `stream_admin/test_local.py`
- `config/stream_admin.yaml`

### Modificar

- `ui/app.py` — nueva pestaña `Stream Admin`, callbacks y controles.
- `docs/changes.md` — estado RF4.
- `README.md` — roadmap refinado.

### No modificar salvo necesidad explícita

- `core/llm_engine.py`
- `motor_ia.py` si existe en variantes legacy.

---

## 7. Dependencias a Evaluar

No instalar nada sin aprobación.

Opciones posibles:

- `google-auth-oauthlib` para OAuth YouTube.
- `google-api-python-client` para YouTube Data API.
- `keyring` para almacenamiento seguro en Windows Credential Manager.
- `cryptography` si se elige archivo local cifrado.
- Twitch futuro: `requests` puro o librería específica validada.

---

## 8. Decisiones MVP Finales

1. **Tokens OAuth:** Usar lo más sencillo para MVP: archivo local `data/stream_admin/oauth_tokens.json`, ignorado por git, sin tokens en logs y con permisos restrictivos cuando sea posible. Migrar a keyring/cifrado después.
2. **Twitch:** Dejar placeholder UI desde MVP, deshabilitado, con texto `Próximamente`.
3. **Mensajes de Kira al chat:** Kira puede escribir mensajes al chat si el streamer habilita esa opción y existen permisos.
4. **Timeout/Ban:** Entran en MVP. Son acciones de alto riesgo y requieren confirmación por defecto.
5. **Presets:** Guardar presets en `config/stream_admin.yaml` durante MVP.

---

## 9. Checklist de Preparación

- [x] RF4 revisado desde `README.md` y `docs/changes.md`.
- [x] Casos de uso propuestos.
- [x] Respuestas del usuario registradas.
- [x] Documento funcional creado.
- [x] Documento de calidad creado.
- [x] Escenarios de prueba creados.
- [x] Handoff creado.
- [x] Dudas de OAuth/tokens resueltas para MVP.
- [x] Implementación iniciada.
- [x] Módulo `stream_admin/` creado.
- [x] Pestaña `Stream Admin` integrada en UI.
- [x] Tests locales RF4 agregados.
- [ ] OAuth YouTube validado con credenciales reales.
