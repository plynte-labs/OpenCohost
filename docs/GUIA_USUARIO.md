# Guía de Uso — OpenCohost (Kira)

Guía rápida para streamers. Todo lo que necesitás saber para usar Kira sin sorpresas.

---

## 1. PTT (Push-to-Talk) — Hablar con Kira

### Cómo funciona

1. Activá PTT en la pestaña **PTT** (switch "PTT ON")
2. La tecla por default es **F8** (podés cambiarla con "Mapear")
3. **Mantené presionada** la tecla mientras hablás
4. **Soltá** cuando terminés — Kira procesa lo que dijiste

### Importante: Buffer inteligente

Kira **no corta tu frase**. Mientras tenés presionada la tecla, acumula todo lo que decís. Al soltar, espera 2 segundos más por si LiveAudio tarda en transcribir.

**Ejemplo:**
```
Presionás F8 → "hola" → "hola cómo estás" → "hola cómo estás hoy"
Soltás F8 → Kira recibe la frase completa: "hola cómo estás hoy"
```

### Qué pasa si Kira está hablando cuando presionás F8

Tu voz **se encola** y se procesa cuando Kira termina. No la interrumpe. Esto evita que Kira se corte a mitad de frase y responda algo incompleto.

### Qué pasa si el chat de YouTube y tu voz llegan al mismo tiempo

**Tu voz tiene prioridad.** El chat espera su turno. Si hay muchos mensajes del chat mientras Kira está ocupada, se agrupan y se envían como una sola consulta después.

### Problemas comunes

| Síntoma | Causa | Solución |
|---------|-------|----------|
| Kira no responde al soltar F8 | LiveAudio no está conectado | Hacé clic en "Conectar LiveAudio" primero |
| Kira responde frases cortadas | Soltaste F8 muy rápido | Esperá un segundo después de terminar de hablar antes de soltar |
| Kira responde a ruido | PTT activado sin LiveAudio | Asegurate de que PTT + LiveAudio estén ambos activos |

---

## 2. Modo Stream Chico — Para streams con poco chat

### Qué hace

Reduce la frecuencia con la que Kira reacciona al chat. Ideal cuando tenés pocos viewers y no querés que Kira hable cada 2 segundos.

### Cómo activarlo

Pestaña **Admin** → switch **"Stream Chico"**

| Modo | Threshold | Cuándo usarlo |
|------|-----------|---------------|
| Normal | 1.0 msgs/seg | Streams con 50+ viewers, chat activo |
| Stream Chico | 0.2 msgs/seg (1 cada 5 seg) | 1-5 viewers, chat tranquilo |

### Se aplica al instante

No hace falta reconectar al chat ni reiniciar nada. El cambio es inmediato.

---

## 3. Cola de Mensajes — Qué pasa cuando Kira está ocupada

Kira procesa **una cosa a la vez**. Si llegan múltiples mensajes mientras está ocupada:

1. **Tu voz (PTT)** → se encola primero (prioridad alta)
2. **Chat de YouTube** → se encola después (prioridad normal)
3. **Si la cola se llena** (5 mensajes) → los extras se guardan en un buffer de acumulación

Cuando Kira termina de procesar todo:
- Revisa si hay mensajes acumulados
- Los agrupa en una sola consulta: *"Mientras procesabas, llegaron estos mensajes del chat: User1: 'x' | User2: 'y'"*
- Envía como una sola respuesta
- Limpia el buffer

**Mensajes viejos (>2 minutos) se descartan automáticamente** porque probablemente ya no son relevantes.

---

## 4. TTS — Dos modos de voz

### Ligero (edge-tts)

- **Requiere internet**
- Voz genérica de Microsoft
- Ideal si no tenés GPU dedicada
- Si se cae internet, falla hasta que vuelva

### Pesado (Qwen3-TTS)

- **Funciona offline** (modelo cacheado localmente)
- Clona la voz del streamer
- Requiere GPU (RTX 3060 o similar)
- Más lento pero no depende de internet

### Cómo cambiar

Pestaña **Audio/TTS** → switch "TTS: Ligero / Pesado"

---

## 5. Filtros del Chat

Kira filtra automáticamente:

- **Emotes puros**: `:bird: :bird: :pogChamp:` → se descartan
- **Spam**: mismo mensaje repetido muchas veces → se descarta
- **Mensajes muy cortos**: menos de 4 palabras → se descartan (no aportan contexto)
- **Carácter repetitivo**: si una sola letra domina el mensaje (ej: `wwwwwwww`, `feeeeeeeee`) → se descarta
- **Gibberish**: texto sin sentido aparente o sin vocales (ej: `jsklsbfkfofii`) → se descarta
- **ASCII art**: dibujos con caracteres especiales → se descartan
- **Mientras Kira habla**: nuevos mensajes se encolan, no se pierden

---

## 6. Perfiles de Personalidad

Kira puede cambiar de personalidad según el contexto del stream.

### Cómo usar

Pestaña **Modelo/Perfil** → seleccioná un perfil del dropdown → Kira adopta esa personalidad al instante.

### Crear un perfil

Botón **"Editar perfiles"** → ventana modal para crear/editar personalidades con prompts custom.

---

## 7. Disco, Cache y Temporales

OpenCohost usa archivos pesados: modelos, audios temporales y caches de IA. Para no saturar el disco C:, podés elegir dónde guardar esos datos.

Archivo:

```txt
config/storage.yaml
```

Por defecto usa `auto`, que mantiene los caches dentro del proyecto o respeta variables existentes del sistema:

```yaml
storage:
  cache_root: "auto"
  temp_root: "auto"
  ollama_models: "auto"
```

Si querés forzar otro disco, editá `config/storage.yaml`:

```yaml
storage:
  cache_root: "/path/to/your/cache"
  temp_root: "/path/to/your/temp"
  ollama_models: "/path/to/ollama/storage"
```

En Windows, podés usar rutas como `D:/OpenCohost/cache` si querés otro disco. <!-- path-ok: storage config example -->

Esto configura por proceso:

- `TEMP` / `TMP` → temporales de Python/TTS
- `HF_HOME` / `HUGGINGFACE_HUB_CACHE` / `TRANSFORMERS_CACHE` → modelos Hugging Face / Qwen
- `TORCH_HOME` → cache de Torch
- `OLLAMA_MODELS` → modelos de Ollama

**Importante:** si Ollama ya estaba abierto antes de iniciar OpenCohost, cerralo y volvé a iniciarlo desde OpenCohost para que tome la ruta configurada.

---

## 8. Atajos Rápidos

| Acción | Cómo |
|--------|------|
| Hablar con Kira | PTT ON + mantené F8 |
| Cambiar tecla PTT | Pestaña PTT → "Mapear" |
| Conectar chat YouTube | Pestaña YouTube → URL del live → "Conectar Chat" |
| Modo Stream Chico | Pestaña Admin → switch "Stream Chico" |
| Limpiar memoria de Kira | Pestaña Audio/TTS → "Limpiar Memoria" |
| Enviar texto manual | Campo de texto abajo de la respuesta de Kira → Enter |

---

## 9. Seguridad y Privacidad

- **Todo es local**: Ollama y Qwen3-TTS corren en tu máquina
- **Sin API keys**: No enviás tu voz ni tus datos a servicios cloud (excepto edge-tts si usás modo ligero)
- **OAuth tokens**: Se guardan en archivo local (ignorado por git). No se suben a ningún servidor
- **Modelos cacheados**: Se descargan una vez y funcionan offline después

---

## 10. Solución Rápida de Problemas

| Problema | Qué hacer |
|----------|-----------|
| Kira no arranca | Verificá que Ollama esté corriendo (`ollama list`) |
| "Ollama no disponible" | Iniciá Ollama y usá el botón de refresh en Modelos |
| PTT no funciona | Verificá que LiveAudio esté conectado (botón "Hablar" verde) |
| TTS timeout | Si usás modo pesado, verificá que `server_qwen.py` esté corriendo |
| Chat YouTube no conecta | Verificá el video_id y que el stream esté en vivo |
| Kira habla sola | Asegurate de que PTT esté ON y que no haya feedback micrófono-parlantes |
| Disco C: al 100% | Configurá `config/storage.yaml` a otro disco y reiniciá OpenCohost/Ollama |
