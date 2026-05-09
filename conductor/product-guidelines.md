# Product Guidelines

## UI/UX Principles
- **Usabilidad sobre decoración**: la funcionalidad prima sobre lo visual
- **Jerarquía sobre ruido visual**: controles primarios grandes y claros, secundarios accesibles pero no dominantes
- **Refactors incrementales seguros**: no rewrites completos sin justificación
- **Estados explícitos**: el usuario siempre debe saber qué está pasando (modelo cargando, mic escuchando, TTS hablando)
- **Desktop ergonomics**: optimizado para monitores de escritorio, no mobile

## UI Layout
- **Pantalla principal**: estado del modelo, entrada de voz/PTT, respuesta de Kira, botón grande Hablar/Detener
- **Panel lateral**: modelo, perfil, audio, YouTube, OAuth, moderación (colapsable)
- **Modo avanzado**: logs, Stream Admin, acciones manuales, debug (opt-in)

## Visual Style
- Dark theme, technical, premium desktop
- Charcoal/grafito de fondo
- Status pills compactas
- Glow sutil solo para estados activos de audio/modelo
- Sensación de control-room / AI console

## Code Conventions
- Python PEP 8
- Docstrings en funciones públicas
- Logging estructurado, no prints
- No silenciar errores con `except: pass`
- Thread safety con locks para estado compartido

## Security
- No commitear secrets, tokens, `.env`
- OAuth tokens con permisos restringidos en filesystem
- Redact de datos sensibles en logs
