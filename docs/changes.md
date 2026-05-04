Este es el **Documento de Especificación de Requerimientos y Arquitectura Sugerida (v1.0)** para el proyecto **VoiceAI - Kira**. Este documento define la transición de un sistema reactivo de bucle abierto a uno de control determinista gestionado por el usuario, optimizado para entornos de alto rendimiento (Streaming/Gaming).

---

## 📄 Especificación de Requerimientos del Sistema (SRS)

### 1. Módulo de Control de Entrada (Control & PTT)
**Propósito:** Sustituir la escucha continua por un sistema de captura bajo demanda para eliminar falsos positivos y reducir carga computacional.

*   **RF1.1 - Gestión de Feature Toggle:** La UI debe incluir un interruptor (Switch) maestro para activar/desactivar la función de **Push-to-Talk (PTT)**. ✅ *Implementado en rama `feature/rf1-ptt-hotkey`.*
    *   **Implementación técnica:**
        *   Se agregará un `CTkSwitch` en `ui/app.py` (frame de control) vinculado a `self.ptt_enabled`.
        *   Cuando PTT está **OFF**, el sistema opera en modo WebSocket continuo (comportamiento actual).
        *   Cuando PTT está **ON**, el WebSocket se pausa y la captura de audio solo ocurre bajo demanda del hotkey.
        *   Estado persistido en memoria de la app (no requiere disco).

*   **RF1.2 - Remapeo de Teclas:** Implementación de un selector de entrada en la interfaz para configurar el **Global Hotkey** (ej. F10, Mouse4) mediante la librería `pynput`. ✅ *Implementado en rama `feature/rf1-ptt-hotkey`.*
    *   **Implementación técnica:**
        *   Dependencia: `pip install pynput`.
        *   Hilo daemon `pynput.keyboard.Listener` + `pynput.mouse.Listener` iniciado en `ui/app.py`.
        *   Dropdown en UI con teclas predefinidas: `F1`–`F12`, `Mouse4`, `Mouse5`, `ScrollLock`, `Insert`.
        *   Eventos `on_press` / `on_release`: inician y detienen la grabación de audio (float32, 24000Hz).
        *   Al soltar la tecla, el buffer de audio se valida (RMS mínimo, duración) y se envía al motor IA como comando `process_context`.
        *   El listener corre en un hilo independiente del mixer de pygame para evitar latencia.
*   **RF1.3 - Lógica de Captura Half-Duplex:** El sistema debe ignorar cualquier entrada de audio mientras el motor TTS esté activo (`self._speaking == True`). El procesamiento del buffer capturado solo iniciará una vez que Kira haya terminado de reproducir su audio actual.
*   **RF1.4 - Integración Silero VAD:** El PTT actuará como una compuerta lógica sobre el flujo de WebSocket existente. Aunque el PTT esté presionado, solo se enviará el segmento de audio a Whisper si Silero detecta voz humana de alta confianza.

### 2. Interfaz y Experiencia de Usuario (UI/UX)
**Propósito:** Minimizar el impacto en los recursos del sistema y evitar interferencias visuales en el monitor principal.

*   **RF2.1 - Arquitectura Multi-Monitor:** El diseño de la interfaz se optimizará para escalado en un segundo monitor, eliminando la necesidad de capas de **Overlay** (DirectX/Vulkan) que consumen ciclos de GPU críticos durante el juego.
*   **RF2.2 - Estado del Pipeline:** La UI debe mostrar indicadores visuales del estado del "grifo" (Tap State): *Escuchando*, *Procesando LLM*, *Sintetizando Voz* y *En Espera*.
*   **RF2.3 - Registro de Acciones (Console Log):** Inclusión de un panel de actividad donde Kira confirme las acciones administrativas ejecutadas (ej: "Título de Twitch actualizado con éxito").

### 3. Procesador Inteligente de Chat (Smart Aggregator)
**Propósito:** Escalar la interacción para audiencias masivas mediante algoritmos de consolidación.

*   **RF3.1 - Filtro de Longitud y Calidad:** Descarte automático de mensajes cortos (ej. "< 5 palabras") o mensajes que solo contengan emojis para limpiar el dataset de entrada.
*   **RF3.2 - Análisis de Sentimiento (Vibe Thermometer):** Implementación de una ventana de tiempo (ej. 120s) donde el LLM evalúe la emoción predominante de la audiencia antes de generar una respuesta global.
*   **RF3.3 - Trigger por Actividad:** Capacidad de activar una respuesta automática de Kira si la velocidad del chat supera un umbral configurable (Mensajes por Segundo), permitiéndole reaccionar a momentos de "Hype".

### 4. Gestión de Stream (Admin Mode)
**Propósito:** Automatizar tareas de producción mediante integración con APIs de terceros.

*   **RF4.1 - Integración Twitch/YouTube API:** Módulo para modificar metadatos del stream (Título, Categoría) mediante botones dedicados en la UI.
*   **RF4.2 - Moderación Automática:** Capacidad del sistema para analizar el sentimiento negativo masivo y activar el "Slow Mode" o "Emote Only Mode" de forma autónoma si el usuario así lo configura.
*   **RF4.3 - Reporte de Analíticas:** Acceso a datos de espectadores concurrentes y eventos de stream para ser inyectados en el contexto del LLM.

---

## 🏗️ Arquitectura Sugerida (Modular Services)

Para garantizar la estabilidad en una **RTX 3060**, se propone una arquitectura de **Microservicios Desacoplados**:

| Componente | Tecnología | Rol |
| :--- | :--- | :--- |
| **Core Orchestrator** | Python / Flask | Gestión de estados, PTT, y lógica de negocio. |
| **LLM Service** | Ollama (Llama 3) | Procesamiento de texto y análisis de sentimiento. |
| **TTS Service** | Qwen3-TTS (Local) | Generación de audio pesado (clonación). |
| **Stream Listener** | WebSockets (Twitch) | Ingesta de chat en tiempo real y filtrado. |
| **Voice Gate** | Silero VAD + Whisper | Transcripción de audio filtrada por hardware. |



---

## 🛠️ Sugerencias para el Agente Constructor

1.  **Manejo de Hilos (Threading):** Es imperativo que el `Global Hotkey Listener` corra en un hilo independiente del `Pygame Mixer` para evitar latencia en la detección de la pulsación.
2.  **Prevención de Bucles (Anti-Loop):** Implementar un "Mute Virtual" por software que limpie el buffer de entrada en el instante exacto en que se presiona el PTT, eliminando residuos de audio previos.
3.  **Persistencia de Contexto:** El historial de chat consolidado debe guardarse en una base de datos ligera (SQLite) para permitir que Kira recuerde temas generales de la audiencia incluso tras un reinicio del servicio.

Este documento sirve como base técnica para la implementación de las siguientes fases del desarrollo de **Kira**.