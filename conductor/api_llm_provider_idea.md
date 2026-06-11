# Idea de Implementación: Integración de API de LLM (Nube)

**Estado**: Pendiente / Idea para revisión futura (Sin prioridad)
**Origen**: Sesión de diseño y debate de arquitectura (2026-06-10)

## Problema
Actualmente, el motor `llm_engine.py` está acoplado de forma rígida a la biblioteca y el demonio local de Ollama. Si queremos habilitar un proveedor de API en la nube (OpenAI, Gemini, Anthropic, OpenRouter, etc.), no podemos hacerlo de forma limpia sin generar código espagueti o romper la estabilidad en vivo del stream.

## Diseño Propuesto

### 1. Abstracción del Cliente (`LlmClient`)
Crear una interfaz abstracta `LlmClient` en `core/llm_client.py`:
- `chat(messages: list[dict], **kwargs) -> str`
- `is_available() -> bool`

Implementar dos clientes concretos:
- `OllamaLlmClient` (Local - encapsula el comportamiento actual de Ollama).
- `ApiLlmClient` (Nube - implementa HTTP/SDK hacia proveedores externos).

### 2. Trazabilidad e Idempotencia (Evitar pérdidas de rastro y doble procesamiento)
- **ID de Correlación**: Generar un ID único por interacción (ej. `corr_8f3a91`) en el punto de entrada de la petición de usuario.
- **Header de Idempotencia**: Enviar el ID como header `X-Idempotency-Key` en la API externa para evitar doble cobro si hay reintentos por caída temporal.
- **Trazabilidad global**: Propagar este ID en logs, archivos temporales (`tts_chunk_{corr_id}_{i}.wav`) y registros de `acciones.jsonl` para depurar fácilmente qué fragmento corresponde a qué interacción.

### 3. Filtros y Robustez en Capa de Red
- **Limpieza de `<think>`**: Filtrar de la respuesta del LLM cualquier bloque de razonamiento `<think>...</think>` (mediante regex) antes de enviarlo al motor de TTS, previniendo que Kira lea sus pensamientos internos.
- **Traducción de Parámetros**: Mapear parámetros específicos de Ollama (`num_predict`, `num_ctx`, `keep_alive`) a sus equivalentes compatibles con API (`max_tokens`, etc.) para evitar excepciones de tipo `Unexpected keyword argument`.
- **Clasificación de Errores**: Capturar errores HTTP de red y mapearlos a excepciones de dominio:
  - `401 Unauthorized` -> Frenar inmediatamente y alertar en la UI.
  - `429 Too Many Requests` -> Reintentar con backoff exponencial con jitter.
  - `5xx / Timeout` -> Reintentar o gatillar fallback local.
- **Circuit Breaker (Disyuntor)**: Si la API de la nube falla de forma reiterada (ej. 3 reintentos) o da error de cuota agotada, el circuito se abre y se fuerza la redirección al LLM local (Ollama) durante 5 minutos para proteger el saldo y evitar loops de spam.

## Fuera de Alcance (Out of Scope)
- El streaming continuo de tokens (LLM streaming a TTS por oraciones) queda aplazado para integrarse dentro del track existente `streaming_speech_pipeline_20260529`. Esta fase 1 del cliente API utilizará llamadas síncronas/bloqueantes estándar apoyándose en proveedores de muy baja latencia.
