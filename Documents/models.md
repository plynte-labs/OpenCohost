Let me first research what small models are available in Ollama that support system roles and work well in Spanish:

Ran command: `python -c "import ollama; models = ollama.list(); print('Installed models:'); [print(f'  {m.model} ({m.details.parameter_size}, {m.details.quantization_level}, {m.size/(1024**3):.1f}GB)') for m in models]"`
Searched web: "ollama small models under 4B parameters 2025 2026 qwen3 phi4-mini llama3.2 system role support Spanish"

Now let me check Ollama's available tags for these models:

Ran command: `python -c "import ollama; mlist = ollama.list(); [print(f'{m[0]} | {m[1].get(\"parameter_size\",\"?\")} | {m[1].get(\"quantization_level\",\"?\")} | {m[1].get(\"family\",\"?\")}') for m in [(mod.model, mod.details.__dict__ if hasattr(mod.details,'__dict__') else {}) for mod in mlist.models]]"`
Edited list_models.py
Ran command: `python temp\list_models.py`

Now I have the full picture. Let me check the Ollama Python API for pull with progress:

Ran command: `python -c "import ollama; help(ollama.pull)" 2>&1 | Select-Object -First 30`

Now let me view the current probar.py structure to plan the UI changes:

Viewed probar.py:58-110
Viewed probar.py:386-470
Viewed probar.py:470-500

Good, I have the full picture. Now let me implement the model management system. I'll add:
1. A curated catalog of recommended models
2. Model selector dropdown + download button in a new row
3. Background download with progress
4. Safe model switching (clears history, waits for idle)

Edited probar.py

Good. Now let me add the UI elements — a model selector row between the top bar and console:

Viewed probar.py:435-550
Viewed probar.py:550-620
Viewed probar.py:620-635

Now I'll add the model management UI row and update the event handler. I need to adjust the grid layout to add a new row for models:

Edited probar.py

Now let me compile-check:

Ran command: `python -c "import py_compile; py_compile.compile(r'probar.py', doraise=True); print('OK')"`

Let me verify the complete file structure makes sense by checking the line count and key sections:

Ran command: `python -c "with open(r'probar.py', encoding='utf-8') as f: lines = f.readlines(); print(f'Total lines: {len(lines)}')"`

Everything compiles. Here's the summary:

---

## ✅ Gestión de Modelos desde la UI

### Modelos Recomendados (Catálogo Curado)

| Modelo | Params | VRAM | Velocidad | Español | Para tu caso |
|---|---|---|---|---|---|
| **Qwen 3 (4B) ★** | 4B | ~2.6GB | ⚡⚡ | Excelente (119 idiomas) | **Mejor opción** |
| Qwen 3 (1.7B) ⚡ | 1.7B | ~1.1GB | ⚡⚡⚡ | Bueno | Ultra rápido |
| Phi-4 Mini (3.8B) | 3.8B | ~2.5GB | ⚡⚡ | Bueno | Buen razonamiento |
| LLaMA 3.2 (3B) | 3B | ~2.0GB | ⚡⚡ | Nativo | Compacto y estable |
| LLaMA 3.2 (1B) ⚡ | 1B | ~1.3GB | ⚡⚡⚡ | Básico | Más rápido posible |
| LLaMA 3 (8B) | 8B | ~4.3GB | ⚡ | Bueno | Tu modelo actual |
| Qwen 2.5 (3B) | 3B | ~1.9GB | ⚡⚡ | Bueno | Probado |
| SmolLM2 (1.7B) ⚡ | 1.7B | ~1.0GB | ⚡⚡⚡ | Básico | Minimalista |

> Todos soportan `role: system` excepto Gemma (excluidos del catálogo).

### Nuevos elementos de UI
- **🧠 Modelo:** Dropdown con los 8 modelos curados
- **⬇️ Descargar:** Descarga desde Ollama en segundo plano con progreso en consola
- **Info label:** Muestra descripción, tamaño y estado (✅ instalado / ❌ no instalado)
- **Protecciones:** No se puede cambiar modelo mientras la IA procesa/habla; descarga no bloquea la UI; limpia historial automáticamente al cambiar

### Mi recomendación: Descarga `qwen3:4b`
Es el mejor balance calidad/velocidad para tu RTX 3060 con XTTS corriendo al mismo tiempo (2.6GB + 3GB = ~5.6GB, te sobran 6GB de VRAM).