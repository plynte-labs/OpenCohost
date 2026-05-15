# VoiceAI — Kira, tu co-host virtual para Stream

## Vision
Aplicación de escritorio que crea un co-host de IA (**Kira**) con procesamiento local-first: LLM y TTS pesado corren en GPU local sin suscripciones ni censura corporativa. Funciones opcionales (Edge-TTS, YouTube Chat, Stream Admin) usan Internet.

## Target Users
- Streamers que quieren un co-host de IA en vivo
- Creadores de contenido que necesitan moderación y gestión de stream automatizada
- Usuarios técnicos que prefieren control local sobre sus modelos de IA

## Core Features
- **Chat con Kira**: texto o voz, la IA responde por voz con TTS
- **2 motores TTS**: Ligero (Edge-TTS, 0% GPU) y Pesado (Qwen3-TTS, clonación zero-shot)
- **Push-to-Talk**: hotkey global para captura de voz
- **WebSocket Live**: conexión a transcripciones en vivo con reconexión automática
- **Pipeline TTS por fragmentos**: la IA empieza a hablar apenas se genera la primera oración
- **Memoria conversacional**: sliding window de 10 turnos
- **Perfiles de personalidad**: 5 perfiles editables desde la UI
- **Catálogo de modelos**: 11 LLMs descargables y cambiables desde la interfaz
- **Smart Chat Aggregator (RF3)**: YouTube Live Chat con filtros, anti-spam, vibe analysis, triggers
- **Stream Admin (RF4)**: YouTube OAuth/API, metadata, moderación, analíticas, mensajes al chat

## Design Principles
- Local-first: todo lo posible corre local
- Graceful degradation: si un servicio falla, el resto sigue funcionando
- UI centrada en Kira: la interacción principal es voz/respuesta
- Config secundaria: settings en panel lateral, no en pantalla principal
- Modo avanzado opcional: logs y debug ocultos por defecto

## Non-Goals
- No es un SaaS cloud
- No requiere suscripción
- No es una app mobile
