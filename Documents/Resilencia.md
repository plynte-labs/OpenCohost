Edited test.md
Viewed README.md:84-97

Let me examine the current state of the relevant sections before applying changes:

Viewed probar.py:1-20
Viewed probar.py:112-300
Viewed probar.py:300-330
Viewed probar.py:530-580
Viewed probar.py:580-660

Good, I have the full picture. Now applying both changes:

1. **`_speaking` flag + WebSocket input rejection** (ignore during processing/speaking)
2. **Chunked TTS with producer-consumer** (sentence-level streaming)

Edited probar.py

Now I notice a potential regex issue — let me check the regex made it through correctly:

Viewed probar.py:256-265

There's an encoding issue with the regex — the `<` got escaped to `<`. Let me fix that and also fix the backslash space:

Edited probar.py

Let me verify the regex line is correct now:

Ran command: `E:\Miniconda\envs\flux_env\python.exe -c "import py_compile; py_compile.compile(r'e:\VoiceAI\probar.py', doraise=True); print('OK')"`

Let me also verify the regex works correctly with a quick test:

Ran command: `E:\Miniconda\envs\flux_env\python.exe -c "import re; texto='Sinceramente, hoy no creo que sea el mejor dia. Pero si tuviera que elegir, diria que si! Estoy listo para pasar un buen rato.'; oraciones = re.split(r'(?<=[.!?;:])\s+', texto); oraciones = [o.strip() for o in oraciones if o.strip() and len(o.strip()) > 2]; print(f'Fragmentos: {len(oraciones)}'); [print(f'  [{i}] {o}') for i, o in enumerate(oraciones)]"`
Edited test_regex.py
Ran command: `E:\Miniconda\envs\flux_env\python.exe e:\VoiceAI\temp\test_regex.py`

Regex splits perfectly. Let me do a final check of the complete file:

Viewed probar.py:243-390

Everything looks clean. Compilation passed, regex works. Here's the summary:

---

## ✅ Resiliencia implementada

### 1. 🚪 Gate de estado en WebSocket
El WebSocket ahora **rechaza transcripciones** en la capa de red cuando:
- `_processing = True` → la IA está pensando (LLM)
- `_speaking = True` → la IA está hablando (TTS reproduciéndose)
- Texto < 4 palabras → filtrado de ruido

Esto evita la acumulación de mensajes en la cola y el ciclo infinito de respuestas desfasadas.

### 2. 🔊 Pipeline TTS por fragmentos (Productor-Consumidor)
La respuesta del LLM se divide en oraciones por puntuación (`.!?;:`), y cada una se sintetiza y reproduce de forma solapada:

```
Tiempo →
[Sintetiza Oración 1]──────[Sintetiza Oración 2]──────[Sintetiza Oración 3]
                        [▶ Reproduce 1]──────────[▶ Reproduce 2]──────────[▶ Reproduce 3]
```

- La IA empieza a hablar **tan pronto como la primera oración está lista**
- Mientras reproduce, el siguiente chunk ya se está generando
- Buffer de 3 chunks máximo para no saturar memoria
- Errores en un chunk individual no matan el pipeline entero
- Si el servidor XTTS se cae, aborta inmediatamente (no intenta más chunks)

### 3. 🛡️ Resiliencia a errores
- Timeout de 45s por chunk + 60s safety timeout global
- `_speaking` flag permite cancelar desde `on_closing`
- Limpieza automática de archivos tras reproducción
- Logging de chunks fallidos vs exitosos

**Reinicia el cliente y prueba.** El servidor no necesita cambios.