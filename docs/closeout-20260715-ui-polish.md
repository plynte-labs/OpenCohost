# Cierre de sesión — 2026-07-15 — UI Polish "Focus Over Panic"

Informe para el owner: qué hicimos, por qué, dónde, qué falta, qué validar y
qué aprendimos. Detalle técnico por fase en
`OpenCohost_UI/docs/design/ui-refresh-20260715-progress.md`, decisiones en
ADR-032 y ADR-033.

## Qué hicimos y por qué

| Qué | Por qué | Dónde |
|---|---|---|
| Fix de la voz que moría tras el 2º ciclo PTT (re-init gateado del mixer) | pygame/SDL sin detección de dispositivo zombie tras churn WASAPI | `opencohost/core/llm_engine.py`, `api/ptt_session.py`, `api/main.py` |
| Health gate de Qwen: nunca-arrancado → `unavailable` | Con Piper/Edge activo el sistema quedaba rojo permanente por un Qwen que no usás | `opencohost/core/health_monitor.py` (TDD, 88/88) |
| Status rail calmo: 4 chips, popovers con "por qué", sin anillo ni avatar | La barra asustaba mostrando errores sin causa ("en rojo ()", "0 MB") | `OpenCohost_UI/src/components/StatusRail.tsx`, `TopBar.tsx` |
| Chat moderno: empty-state, aviso de viewers anclado, KiraFace, ancho completo, mic con llenado, auto-scroll + píldora | El chat se veía mockeado y desaprovechaba espacio; faltaba feedback del mic | `ConversationPanel.tsx`, `ui/KiraFace.tsx` |
| Markdown seguro en respuestas de Kira | El LLM genera **negritas**/tablas/código que se veían crudas | `ui/Markdown.tsx` (react-markdown sin rehype-raw, sin `<img>`) |
| Alertas: 1 componente, 3 estilos (sereno/marcado/contorno) desde el engrane | Mantener sin color pero con presencia elegible; marcado con doble línea para no parecer botón | `ui/Alert.tsx`, `theme/useAlertStyle.ts`, `styles.css` |
| Form de Perfil reordenado (guardar al final, ritmo con aire) | Jerarquía rota: el botón de guardar quedaba enterrado | `AgendaPanel.tsx` |
| Paleta de comandos `/` y `!` — 7 comandos maquetados | Commodity tipo CLI desde el chat, sin llamar al LLM | `ComposerCommandPanel.tsx`, `src/components/commands/` |
| Colapsables persistentes + panel Controles agrupado | El estado de navegación se perdía; Controles eran 8 cards sueltas | `ui/Collapsible.tsx`, `ControlsPanel.tsx` (nuevo), `StreamPanel.tsx` |
| Hover de perfiles (700ms) con prompt real + lápiz de edición cableado | Los perfiles parecían inertes; el editor existía pero era inalcanzable | `Sidebar.tsx`, `ProfilePlaylist.tsx`, `api/profiles.ts` |
| Audit de sanitización (read-only) + hardening de imágenes remotas | Primer componente que convierte salida del LLM en DOM rico | ADR-032 |
| VRAM real: `nvidia-ml-py` instalado en flux_env | `VRAMGuard` degradaba a "0 MB" sin pynvml | env local + candidato #9 del proposal |
| Welcome carousel restaurado (rediseño + 4 ilustraciones + crédito Franguh) | Un fixer lo revirtió por error de clasificación de scope; se reconstruyó todo | `WelcomeCard.tsx`, `public/welcome/`, `TopBar.tsx` |

Cifras: la suite de UI pasó de 533 a **626 tests (66 archivos)**, build verde.
Backend: 88/88 (health) + 291 (suite del fix TTS) verdes.

## DEUDAS — qué falta validar (importante, owner)

1. **PTT + LiveAudio sin corte de voz — NO VALIDADO EN RUNTIME.** El fix está
   commiteado con tests unitarios verdes, pero nadie corrió el escenario real:
   LiveAudio + 2+ ciclos PTT + turno tipeado → la voz debe seguir y el log debe
   mostrar `Audio device re-inicializado (recovery)`. Registrado en
   `AGENT_HANDOFF.md` y Engram. **El commit no cierra esta deuda.**
2. **Gate 3 PTT**: prueba del watchdog kill-mid-hold, pendiente de antes.
3. **Motor verde + VRAM real**: requieren reiniciar el backend (gate de Qwen
   nuevo + pynvml recién instalado). Verificación visual de 1 minuto.
4. **Auth de la API (P1 del audit)**: endpoints mutantes sin token por defecto.
   OK en loopback single-operator; **decisión obligatoria antes de exponer
   fuera de localhost**.
5. **Validación visual general**: todo el trabajo de UI se verificó por tests
   jsdom + build, sin browser/Tauri real (sesión headless). Scrollbars,
   fades, llenado del mic y hover cards merecen un vistazo humano.

## Dudas mías (para cuando vuelvas)

- `/acciones`: el endpoint ya acepta `filter_policy` — falta TU decisión de qué
  preset corresponde al switch del comando.
- El copy del hover de perfiles dice "se edita desde Controles"; ahora también
  se edita con el lápiz de la lista. ¿Lo actualizamos o queda como guiño?
- `nvidia-ml-py`: ¿lo promovemos a dependencia base en `pyproject.toml`
  (candidato #9) o extra propio de GPU?
- Los 7 comandos están maquetados: ¿cuál cableamos primero? Mi sugerencia:
  `/agenda` (calca el contrato del CLI).

## Desviación de proceso documentada

El gate `gentle-ai review validate --gate pre-commit` no encontró lineage de
review (el ciclo bounded nunca se inició para este trabajo). Se commiteó igual
por instrucción explícita del owner ("cerrar todo y empezar a commitear",
trabajo administrativo), apoyado en los reviews adversariales que SÍ corrieron
en la sesión (review opus de diff completo con findings aplicados + audit de
seguridad read-only). Si se quiere el receipt formal, correr
`gentle-ai review start` sobre el árbol commiteado en una sesión futura.

## Aprendizajes de la sesión

1. **Baseline antes de escribir.** Un reviewer sobre árbol sucio clasificó
   trabajo previo del owner como "fuera de scope" y un fixer lo borró
   (incluidos 4 PNGs con `rm -f`, irrecuperables de git). Se restauró todo
   desde los transcripts de los propios agentes. Regla permanente: capturar
   `git status` antes de la fase 1; lo que ya estaba sucio es del owner — se
   reporta, no se toca; un fixer jamás borra untracked.
2. **Los transcripts son backups.** El diff del reviewer y el `cat` del fixer
   contenían byte a byte todo lo destruido. Antes de declarar pérdida, minar
   los transcripts.
3. **Writers paralelos = ownership explícito de archivos** + un solo
   `pnpm build` del orquestador al final (el prebuild regenera un archivo
   compartido y puede pisarse).
4. **Comandos declarativos escalan.** El framework de steps (7º comando costó
   ~1/10 del primero) confirma la apuesta por primitivas reutilizables.
5. **La honestidad del estado vale más que el verde cosmético**: "VRAM no
   disponible" y "voz (Qwen) sin iniciar" explican; "0 MB" y "()" asustan.
6. **Economía de modelos** (pedido del owner): sonnet para exploración/mecánica,
   opus para writers/reviews, fable solo diseño/síntesis pesada — funcionó sin
   pérdida de calidad perceptible.

## Estado de git al cierre

Ambos repos commiteados en `codex/ui-ux-audit-proposal-20260709` (sin push,
como siempre — push es decisión del owner). Ignorados de por vida:
`config/` (contiene `api_tokens.json` — secretos), `.pnpm-store/`, `.atl/`,
`src-tauri/backend.config.json` (rutas locales de máquina).
