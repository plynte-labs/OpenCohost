# RF4 — Requerimientos de Calidad y No Funcionales (Stream Admin Mode)

**Código:** RF4-QA  
**Módulo:** Gestión de Stream / Admin Mode  
**Versión:** 0.1  
**Estado:** Diseño refinado  
**Fecha:** 2026-05-04

---

## 1. Requerimientos No Funcionales

### RNF4.1 — Seguridad OAuth y Protección de Tokens

| Campo | Descripción |
|-------|-------------|
| **ID** | RNF4.1 |
| **Categoría** | Seguridad |

**Descripción:**  
RF4 debe implementar autenticación OAuth 2.0 con el menor privilegio posible, empezando por modo solo lectura. Los tokens no deben exponerse en logs, UI, commits ni archivos de configuración legibles sin protección.

**Criterios:**

- Solicitar scopes de lectura antes que scopes de escritura.
- Solicitar scopes de escritura solo si el streamer habilita funciones que modifican el stream.
- No imprimir access tokens ni refresh tokens en logs.
- Para MVP, guardar tokens en `data/stream_admin/oauth_tokens.json`, ignorado por git y con permisos locales restrictivos cuando sea posible.
- Migrar almacenamiento de tokens a Windows Credential Manager/keyring o archivo cifrado en una fase posterior.
- Permitir revocar/desconectar cuenta desde UI.
- Fallar en modo seguro si no hay token válido.

---

### RNF4.2 — Control del Streamer y Seguridad Operativa

| Campo | Descripción |
|-------|-------------|
| **ID** | RNF4.2 |
| **Categoría** | Seguridad Operativa |

**Descripción:**  
El streamer debe conservar control total sobre acciones que afecten metadata o moderación.

**Criterios:**

- Aprobación requerida por defecto para cambios de metadata.
- Modo automático desactivado por defecto.
- Switch maestro para desactivar toda moderación automática.
- Acciones de alto riesgo (`timeout`, `ban`) requieren confirmación por defecto.
- Toda acción debe poder auditarse después.

---

### RNF4.3 — Robustez ante Fallos de API

| Campo | Descripción |
|-------|-------------|
| **ID** | RNF4.3 |
| **Categoría** | Robustez |

**Descripción:**  
RF4 debe tolerar errores de red, rate limits, tokens expirados, scopes insuficientes y APIs no disponibles sin cerrar VoiceAI ni bloquear RF3/Core.

**Criterios:**

- Reintentos con backoff para errores transitorios.
- Mensajes claros para tokens expirados o permisos insuficientes.
- Deshabilitar botones peligrosos si el proveedor no está conectado.
- Mantener la UI responsiva durante llamadas API.
- RF4 no debe bloquear generación LLM/TTS.

---

### RNF4.4 — Rate Limits y Cuotas

| Campo | Descripción |
|-------|-------------|
| **ID** | RNF4.4 |
| **Categoría** | Rendimiento/API |

**Descripción:**  
RF4 debe minimizar llamadas a APIs externas para evitar bloqueos, cuotas agotadas o rate limits.

**Criterios:**

- Cache de metadata leída por intervalo configurable.
- Actualización de analíticas configurable.
- No hacer polling agresivo por defecto.
- Agrupar cambios cuando sea posible.
- Registrar rate-limit como evento operativo, no como fallo fatal.

---

### RNF4.5 — Privacidad y Persistencia Local

| Campo | Descripción |
|-------|-------------|
| **ID** | RNF4.5 |
| **Categoría** | Privacidad |

**Descripción:**  
Los datos administrativos del canal, tokens, analíticas y logs deben almacenarse localmente y con límites claros.

**Criterios:**

- No enviar datos a servicios externos salvo APIs oficiales del proveedor seleccionado.
- No subir tokens ni logs sensibles a repositorio.
- Separar configuración no sensible (`config/stream_admin.yaml`) de secretos.
- Logs de acciones deben omitir secretos y datos privados innecesarios.

---

### RNF4.6 — Rendimiento Local

| Campo | Descripción |
|-------|-------------|
| **ID** | RNF4.6 |
| **Categoría** | Rendimiento |

**Descripción:**  
RF4 no debe añadir carga GPU ni competir con LLM/TTS.

**Criterios:**

- Cero modelos adicionales.
- Operaciones API en hilos/background workers.
- No bloquear `mainloop` de CustomTkinter.
- Uso de memoria estable durante sesiones largas.

---

## 2. Criterios de Aceptación

Para considerar RF4 listo para MVP seguro:

- [ ] Existe módulo `stream_admin/` separado o decisión arquitectónica documentada si se integra distinto.
- [ ] Existe `config/stream_admin.yaml` con defaults seguros.
- [ ] YouTube OAuth read-only funciona sin exponer tokens.
- [ ] La UI muestra cuenta/canal conectado y scopes activos.
- [ ] La UI puede leer título/categoría/descripción/tags actuales.
- [ ] La UI no permite escritura si no se solicitaron scopes de escritura.
- [ ] Kira puede sugerir metadata, pero no aplicarla sin aprobación por defecto.
- [ ] El streamer puede modificar sugerencias antes de aplicar.
- [ ] Existen presets de títulos/categorías persistidos localmente.
- [ ] Moderación automática está apagada por defecto.
- [ ] `Solo alertas`, `Confirmación requerida` y `Automático` están disponibles.
- [ ] Slow Mode puede activarse/desactivarse manualmente si el proveedor lo soporta.
- [ ] Anuncios de moderación al chat son opcionales y apagados por defecto.
- [ ] Analíticas se muestran e inyectan con frecuencia configurable.
- [ ] RF4 registra acciones en `logs/acciones.jsonl`.
- [ ] Fallos de API/token no crashean VoiceAI.

---

## 3. Restricciones y Supuestos

1. YouTube es el proveedor inicial.
2. Twitch debe quedar contemplado por interfaz, pero no necesariamente implementado en MVP.
3. OAuth debe priorizar modo solo lectura antes de escritura.
4. RF4 puede depender de señales RF3, pero no debe acoplarse a clases internas de RF3.
5. RF4 no modifica `core/llm_engine.py`.
6. RF4 reutiliza un único LLM mediante `llm_interface` inyectada.
7. El streamer puede desactivar cualquier automatización.
