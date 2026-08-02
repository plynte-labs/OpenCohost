# Agenda bulk-import prompt template

Copy-paste block for asking an external agent to generate a topic list for the OpenCohost agenda.

**Verified against the live parser 2026-07-30** — the Tauri front's `parseBulkLine`
(`OpenCohost_UI/src/components/AgendaPanel.tsx:554`) and the backend guardrails
(`KiraAgendaController.sanitize_topic_text`, `kira_agenda_controller.py:558`). Re-verify before
trusting it if either file changed.

> ⚠ **The Tauri and CTK bulk formats are different parsers.** CTK takes multi-line labelled blocks
> (`Tema:` / `Ángulo:` / `Prioridad:` / `Tags:`). Tauri takes **one pipe-separated line per topic**.
> This template is the **Tauri** format. Pasting CTK blocks into Tauri produces one garbage topic
> per line, silently.

> ⚠ **The parser's own docstring is stale.** It says field 4 is `tags`; the code assigns it to
> `constraints` (`:558-562`). Field 4 is constraints.

---

## The prompt

```
Necesito una lista de N temas para la agenda de un co-host de streaming en español.

FORMATO DE SALIDA — obligatorio, sin excepciones:
Una línea por tema, cuatro campos separados por el carácter |

Título | Ángulo | Prioridad | Restricción1, Restricción2

REGLAS DE CADA CAMPO:
1. Título — obligatorio. Máximo 90 caracteres. Si falta, la línea se descarta en silencio.
2. Ángulo — opcional pero recomendado. Máximo 1000 caracteres. Es la bajada:
   desde qué punto de vista encarar el tema, no un resumen.
3. Prioridad — exactamente una de: alta, normal, baja
   Cualquier otra palabra se convierte en "normal" sin avisar.
4. Restricciones — separadas por coma. Máximo 12 por tema, 120 caracteres cada una.
   Son instrucciones de tono o límites ("no spoilers", "sin nombres propios").
   Las que sobren después de la 12 se descartan en silencio.

PROHIBIDO — estas cosas hacen que el tema sea RECHAZADO por completo:
- Emojis o símbolos decorativos de cualquier tipo. Ninguno. Ni ✓ ni ★ ni 🎮.
- Estas palabras sueltas, en cualquier idioma o contexto:
  function, class, import, from, select, insert, update, delete, drop, script, console.log
  (Ojo con "drop" y "from": aparecen naturalmente en temas de música y de inglés.
   Si necesitás la idea, parafraseala.)
- Etiquetas HTML, bloques de código con ```, flechas =>, o tres o más de { } ; seguidos.
- Saltos de línea dentro de un tema. Un tema = una línea.

NO INCLUYAS: cantidad de turnos, ritmo, ni longitud de respuesta.
Esos son controles globales de la sesión, no van por tema. Si los ponés, se ignoran.

CONTENIDO:
[describí acá el dominio, el tono y la audiencia]

EJEMPLO DE SALIDA VÁLIDA:
La nostalgia noventera en internet | Por qué volvemos a símbolos viejos cuando el presente pesa | alta | sin nombres propios, tono liviano
Mods como cultura popular | Comunidades chicas que terminan definiendo gustos enormes | baja | no spoilers
Trabajo remoto y ciudades vacías | Qué le pasa a un barrio cuando su oficina se apaga | normal |

Devolveme SOLO las líneas, sin numerar, sin viñetas, sin encabezado y sin explicación.
```

---

## Why each rule is there (the verified source)

| Rule | Enforced at | Failure mode |
|---|---|---|
| Title required, ≤ 90 chars | `TITLE_MAX_CHARS = 90`; `parseBulkLine:557` returns `null` on blank | blank → **silently dropped**; too long → `ValueError`, topic rejected |
| Angle ≤ 1000 chars | `ANGLE_MAX_CHARS = 1000` | `ValueError`, topic rejected |
| Priority ∈ {alta, normal, baja} | `PRIORITY_ORDER`; `normalize_priority:585` | anything else → **silently becomes `normal`** |
| ≤ 12 constraints, ≤ 120 chars each | `MAX_CONSTRAINTS = 12`, `CONSTRAINT_MAX_CHARS = 120` | extras → **silently dropped** |
| No emoji/symbols | `contains_emoji_or_symbol:577` — `ord > 0xFFFF` or `0x2600–0x27BF` | `ValueError`, topic rejected |
| No code-looking text | `CODE_PATTERNS:333` | `ValueError`, topic rejected |
| One line per topic | newlines collapsed by `sanitize_topic_text:559`; the front splits on `\n` | a multi-line topic becomes several broken topics |
| No turns/rhythm/length per topic | bulk path hardcodes `response_length: "normal"` (`AgendaPanel.tsx:567`) | **silently ignored** |

**Note the asymmetry that makes this worth being strict about:** some violations raise an error you
will see, and others are dropped in silence. A bad priority, a 13th constraint, and a blank title
all fail *quietly* — you get fewer or different topics than you asked for and nothing tells you.

The word list is the least obvious trap. `CODE_PATTERNS` matches whole words case-insensitively, so
a legitimate Spanish topic about music containing "el drop" or an English phrase with "from" is
rejected as code. That is the guardrail working as designed; it just has no idea about your domain.

---

## Update log

- **2026-07-30**: Created. Verified against `parseBulkLine` and `sanitize_topic_text`.
