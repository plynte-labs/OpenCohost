# Arquitectura de VoiceAI

La aplicación se ha modularizado desde un único archivo monolítico (`main.py`) hacia una estructura en paquetes modernos para mejorar su mantenibilidad, legibilidad y escalabilidad.

## Estructura de Directorios

```plaintext
VoiceAI/
├── config/
│   ├── __init__.py
│   ├── logger.py       # Configuración global del sistema de logging estructurado
│   └── settings.py     # Constantes globales, rutas, catálogo de modelos y prompt del sistema
│
├── core/
│   ├── __init__.py
│   ├── llm_engine.py   # Motor de inteligencia artificial (hilo en segundo plano que gestiona Ollama y TTS)
│   └── profiles.py     # Lógica para la carga y guardado de perfiles (personalidades/prompts)
│
├── ui/
│   ├── __init__.py
│   ├── app.py              # Interfaz gráfica principal (VocalAIApp), chat manual, audio capture y websockets
│   └── profiles_window.py  # Ventana modal para crear, editar y eliminar perfiles de IA
│
├── docs/
│   └── architecture.md # Este documento
│
├── temp/               # Directorio de archivos temporales (fragmentos de audio generados)
├── logs/               # Archivos de registro (logs) generados por la aplicación
│
└── main.py             # Punto de entrada (entrypoint) que inicializa y lanza la aplicación
```

## Flujo de Información

1. **Punto de Entrada**: Al ejecutar `python main.py`, se importa la clase `VocalAIApp` desde el paquete `ui`. Se configura el tema de `customtkinter` y se arranca el loop de la interfaz.
2. **Configuración**: Todos los componentes leen parámetros (rutas de directorios, catálogo de LLMs, URL del websockets, etc.) desde `config.settings`.
3. **Registro (Logging)**: Se utiliza una instancia global proporcionada por `config.logger` para registrar eventos concurrentemente tanto en consola como en archivo en la carpeta `logs/`.
4. **Motor de IA (Backend)**: El objeto `MotorVocalIA` en `core.llm_engine` corre en su propio hilo. Se comunica de forma asíncrona mediante una cola (`command_queue`) desde la interfaz para procesar audios, textos de la red o comandos manuales. El motor utiliza `Ollama` para la inferencia de lenguaje y manda las oraciones a generar por TTS local o HTTP.
5. **Interfaz de Usuario (Frontend)**: Todo el frontend reside en `ui`. `app.py` administra los hilos de red, los eventos de hardware (micrófonos) y envía eventos a la cola del Motor IA, mientras que actualiza la UI visual leyendo del callback del motor.

## Agregar Nuevas Funciones

- **Para agregar una nueva configuración o un nuevo modelo de Ollama**, modifique `config/settings.py`.
- **Para actualizar la forma en la que la IA responde o maneja memoria**, edite `core/llm_engine.py`.
- **Para cambiar el comportamiento visual, agregar botones o ventanas**, busque en `ui/app.py` o cree un nuevo archivo `.py` en la carpeta `ui/` (como lo hace `profiles_window.py`).
- **Para lógica de negocio o de backend adicional (ej: integración con otra API externa)**, considere agregar un archivo en la carpeta `core/` y enlácelo con la UI a través de `app.py`.
