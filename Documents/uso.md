La razón por la que tu GPU (RTX 3060) está al 100% es porque estás ejecutando **tres redes neuronales pesadas simultáneamente**, algo que normalmente requeriría un clúster de servidores, pero lo estás haciendo todo localmente en una sola tarjeta gráfica. 

Aquí tienes el desglose exacto de lo que más consume recursos, de mayor a menor impacto:

### 1. 🥇 El devorador absoluto: Coqui XTTSv2 (Servidor de Voz)
*   **Impacto:** Extremo (~3-4 GB de VRAM fijos + 100% de uso de cálculo al hablar).
*   **Por qué:** XTTS no es un sintetizador de voz tradicional robótico. Es un modelo fundacional de clonación de voz "Zero-Shot". Para generar cada palabra con la entonación perfecta de "Kira", hace cálculos matemáticos inmensos.
*   **El problema:** A diferencia de Ollama, el servidor de XTTS está "siempre activo" en VRAM mientras esté encendido.

### 2. 🥈 El LLM: Ollama (Llama/Qwen)
*   **Impacto:** Alto (~1.5 GB a 4.5 GB de VRAM dependiendo del modelo).
*   **Por qué:** Generar texto letra por letra requiere que toda la red se active en paralelo. Cuando "Kira" está pensando qué responder, tu GPU se pone al 100% para escupir los tokens lo más rápido posible.
*   **Lo bueno:** Gracias a que pusimos el `keep_alive=0` hace un momento, ahora al menos se descarga de la VRAM cuando no se usa.

### 3. 🥉 El Transcriptor: Faster-Whisper (LiveAudio)
*   **Impacto:** Medio (~1 a 2 GB de VRAM fijos).
*   **Por qué:** Está escuchando tu micrófono constantemente. Cada vez que hablas, hace inferencia en tiempo real para convertir el audio a texto. Como el loop es continuo, siempre mantiene a la GPU trabajando un poco (spikes de uso cada 2-3 segundos).

---

### ¿Cómo puedes aliviar tu gráfica?

Si notas que la PC se congela, los streams pierden cuadros (FPS drops), o se calienta mucho, puedes hacer esto para optimizarla:

1.  **Cambia Whisper a CPU:** En tu proyecto `LiveAudio`, abre tu configuración y cambia el dispositivo de Whisper de `"cuda"` a `"cpu"`. Usar la versión `base` o `small` en CPU es muy rápido y libera 2GB completos de VRAM y carga de GPU que le puedes dejar a XTTS.
2.  **Usa modelos LLM "Cuantizados" (más pequeños):** Sigue usando modelos de la familia `1.5b` a `3b` (como `llama3.2:1b` o `qwen2.5:3b`). Evita cargar modelos de 8B mientras haces stream y usas XTTS.
3.  **Límites en OBS:** Si estás haciendo stream al mismo tiempo, tu GPU también está codificando video (NVENC) y renderizando el juego. Es vital que limites los FPS de tu juego (ej. a 60 FPS fijos) para que la GPU no intente dar el 100% al juego, dejando un 10-20% libre para la IA.