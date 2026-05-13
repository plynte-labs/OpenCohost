# Current UI Inventory — Product Classification Draft

This inventory is the safety net for the product UI refactor. Nothing should be moved or removed until it appears here with an explicit destination.

## Current top-level layout

| Current area | File/class | Behavior | Proposed category | Preserve? | Risk / gotcha |
|---|---|---|---|---|---|
| Status bar pills | `ui/app_shell.py`, `ui/status_bar.py` | Shows model/TTS/chat/mic plus OAuth/memory/moderation pills | Kira left panel + System summary | Yes | Some pills are outside `StatusBar`; unify carefully |
| Top status from `UIState` | `ui/status_bar.py` | Reflects model/mic/TTS/chat and pipeline state | Kira left panel / System summary | Yes | Duplicates some lower Kira state labels |
| `Mostrar logs` switch | `ui/app_shell.py` | Toggles advanced logs panel | Logs | Yes | Logs should default hidden, not dominate product UI |
| `Compacto` switch | `ui/app_shell.py` | Toggles compact mode | System / View controls | Yes | Verify current behavior before moving |
| Main nav: `Kira`, `Stream Admin` | `ui/app_shell.py` | Switches main content frame | Replace with product panels | Yes, but redesign | Stream Admin currently competes with Kira as a top-level product view |
| Kira response textbox | `ui/app_shell.py`, `AdvancedModePanel` receives reference | Displays last Kira response / logs | Kira left panel | Yes | Must remain the emotional/product center |
| Kira response log parser | `ui/advanced_panel.py` | Updates Kira response from `[Kira]:` log lines | Kira left panel / Logs bridge | Yes | Coupled to log text format; preserve until replaced intentionally |
| Manual chat input + `Enviar a IA` | `ui/app_shell.py` | Sends manual prompt to LLM | Kira left panel or Agent quick action | Yes | Should feel like talking to Kira, not debug input |

## Current configuration tabs

| Current UI element | File/class/function | Current behavior/callback/state | Proposed category | Preserve? | Risk / gotcha |
|---|---|---|---|---|---|
| `Modelo/Perfil` tab | `ui/app_shell.py` | Contains model and profile panels | Agent / Brain | Yes | Correct grouping, but label should become product language |
| Model selector | `ui/model_panel.py` | Selects active Ollama model | Agent / Brain | Yes | Live Ollama state can make tests environment-dependent |
| Download/change model button | `ui/model_panel.py` | Downloads or switches model | Agent / Brain | Yes | Needs clear disabled/ready states |
| Model info/progress | `ui/model_panel.py` | Shows model description/download progress | Agent / Brain | Yes | Keep visible but not louder than Kira state |
| Profile selector | `ui/profile_panel.py` | Chooses Kira persona/profile | Agent / Brain | Yes | Must sit next to model and memory; together define the agent |
| Edit profiles button | `ui/profile_panel.py`, `ui/profiles_window.py` | Opens profile editor | Agent / Brain | Yes | Ensure dialog ownership still works after move |
| Audio device selector | `ui/app_shell.py` | Chooses microphone device | Voice / Input | Yes | Device index parsing is fragile; preserve exact behavior first |
| `Grabar` button | `ui/app_shell.py` -> voice recording methods | Records voice reference | Voice / Input | Yes | User specifically called this out; do not hide too deep |
| Recording implementation | `ui/app_shell.py`, `ui/voice_control.py` | Visible `Grabar` uses AppShell recording; VoiceControlPanel also has recording methods | Voice / Input | Yes, consolidate later | Duplicate implementation; verify behavior before deduping |
| `Cargar WAV` button | `ui/app_shell.py` | Loads reference voice file | Voice / Input | Yes | Pair with `Grabar` as reference voice workflow |
| `Conectar LiveAudio` button | `ui/app_shell.py`, `VoiceControlPanel` | Toggles STT WebSocket | Voice / Input | Maybe consolidate | Duplicates primary `Hablar`; preserve callback path first |
| TTS mode switch | `ui/app_shell.py` | Switches ligero/pesado | Voice / Input | Yes | Qwen heavy mode depends on reference voice |
| `Limpiar Memoria` button | `ui/app_shell.py` -> `llm_engine.clear_history` | Clears conversation history | Agent / Brain | Yes | User noted memory belongs with model/profile, not Voice |
| PTT switch | `ui/app_shell.py`, `ui/ptt_manager.py` | Enables/disables PTT | Voice / Input | Yes | Preserve F8/transition fixes |
| PTT hotkey label/map button | `ui/app_shell.py`, `ui/ptt_manager.py` | Shows/maps hotkey | Voice / Input | Yes | Global hotkey handling is high-risk; move container only first |
| YouTube URL/video ID | `ui/app_shell.py`, `SmartAggregatorUI` | Configures live chat source | Stream | Yes | Should sit with Smart Aggregator/Stream Admin |
| `Conectar Chat` button | `ui/app_shell.py`, `SmartAggregatorUI` | Connects YouTube chat | Stream | Yes | Avoid duplicate with Stream Admin authenticated chat confusion |
| Max messages/user | `ui/app_shell.py`, `SmartAggregatorUI` | Anti-spam user limit | Stream | Yes | Label needs product explanation |
| Admin side OAuth summary | `ui/app_shell.py` | Status text pointing to Stream Admin | Stream | Maybe | Might become redundant once Stream panel is classified |
| Admin side moderation summary | `ui/app_shell.py` | Status text pointing to Stream Admin | Stream | Maybe | Keep as compact Stream summary if useful |
| `Registrar logs en avanzado` switch | `ui/app_shell.py` | No command/read usage found in current inventory | Logs / System | Remove or wire | Obvious phantom control unless later code proves otherwise |

## Voice/Kira runtime panel

| Current UI element | File/class/function | Current behavior/callback/state | Proposed category | Preserve? | Risk / gotcha |
|---|---|---|---|---|---|
| Voice primary button | `ui/voice_control.py` | Main voice interaction button | Kira left panel / Voice quick action | Yes | Keep as quick action near Kira |
| RMS bar | `ui/voice_control.py` | Shows audio level/listening animation | Kira left panel | Yes | Helps make Kira feel alive |
| Kira voice/TTS/memory/chat labels | `ui/voice_control.py` exposed in `app_shell.py` | Shows sub-states | Kira left panel | Yes | Consolidate with status pills to avoid duplicate state |

## Stream Admin

| Current UI element | File/class/function | Current behavior/callback/state | Proposed category | Preserve? | Risk / gotcha |
|---|---|---|---|---|---|
| Stream Admin full view | `ui/stream_admin_ui.py` | Large operational panel | Stream | Yes | Move under Stream category; do not rewrite internals first |
| Stream tabs: Conexión/Metadata/Moderación/Chat/Estado | `ui/stream_admin_ui.py` | Organizes RF4 controls | Stream | Yes | Already well scoped internally, but too visually dominant globally |
| YouTube read/write OAuth buttons | `ui/stream_admin_ui.py` | Connect/reconnect/revoke/disconnect OAuth | Stream | Yes | Security-sensitive; preserve exact behavior |
| OAuth client/secret entries | `ui/stream_admin_ui.py` | Saves OAuth config | Stream / System | Yes | Secrets handling; do not expose in Kira left panel |
| Metadata read/suggest/apply/reject | `ui/stream_admin_ui.py` | Stream title/category/tags/description workflow | Stream | Yes | High value but advanced; belongs in Stream operations |
| AutoMod settings | `ui/stream_admin_ui.py` | Runtime moderation mode/announcements | Stream | Yes | Must remain explicit to avoid accidental moderation |
| Manual moderation user/reason/timeout/ban | `ui/stream_admin_ui.py` | Proposes high-risk moderation | Stream | Yes | High-risk actions need confirmations preserved |
| Authenticated chat connect | `ui/stream_admin_ui.py` | Connects current live chat via API | Stream | Yes | Potential overlap with RF3 `Conectar Chat`; clarify in UI copy |
| `Permitir mensajes`, `Stream Chico` | `ui/stream_admin_ui.py` | Runtime stream behavior switches | Stream | Yes | Product useful; can surface state on Kira panel |
| `Simular Chat`, `Enviar al chat`, `Forzar Kira` | `ui/stream_admin_ui.py` | Manual stream/co-host actions | Stream | Yes | Keep as Stream tools, not primary Kira chat |
| Twitch Próximamente button | `ui/stream_admin_ui.py` | Disabled placeholder | Stream | Maybe | Keep only if clearly future; otherwise hide to reduce clutter |
| Recent users moderation list | `ui/app_shell.py`, `ui/stream_admin_ui.py` | Dynamic timeout/ban rows from tracked chat users | Stream | Yes | Requires authenticated chat/channel IDs; high-risk UX must stay explicit |

## Logs / advanced panel

| Current UI element | File/class/function | Current behavior/callback/state | Proposed category | Preserve? | Risk / gotcha |
|---|---|---|---|---|---|
| Log General | `ui/advanced_panel.py` | General application logs | Logs | Yes | Hide by default |
| Kira Acciones | `ui/advanced_panel.py` | Agent/action trace | Logs / Kira activity history | Yes | Could later become a friendly activity feed |
| YT Chat log | `ui/advanced_panel.py` | Raw/filtered chat log | Logs / Stream diagnostics | Yes | Do not put raw chat in primary view |
| Stream Log | `ui/advanced_panel.py` | Stream Admin log | Logs / Stream diagnostics | Yes | Preserve for troubleshooting |

## Missing planned product area

| Needed UI element | Proposed file/module | Behavior | Proposed category | Preserve? | Risk / gotcha |
|---|---|---|---|---|---|
| Avatar mode selector | `ui/avatar_panel.py`, `avatar/avatar_config.py` | none / placeholder / 2D / 3D / OBS | Avatar / OBS | New | Future modes must not appear as working if not implemented |
| Avatar state bridge | `avatar/avatar_state.py` | `idle`, `listening`, `thinking`, `speaking`, `error` | Avatar / OBS | New | Should consume UIState/motor events, not own business logic |
| OBS overlay output | `avatar/obs_overlay.py` | Future browser source/window endpoint | Avatar / OBS | New | Keep as placeholder/config first unless implementation is scoped |
| Visual preview | `ui/avatar_panel.py` and Kira left panel | Shows placeholder/current avatar | Kira left panel + Avatar / OBS | New | Must be lightweight; no heavy rendering during inference |
| Storage/cache UI | Not found in inspected UI | Storage config exists in `config/storage.py`/`config/storage.yaml`, but no visible UI control | System | New / future | Add only after storage behavior is stable and documented |

## Obvious duplicates / phantom controls

| Item | Finding | Refactor action |
|---|---|---|
| `Hablar` vs `Conectar LiveAudio` | Both appear to toggle the same LiveAudio WebSocket path | Keep one primary Kira quick action and one detailed Voice/Input status/control if needed |
| Recording code | Visible `Grabar` uses AppShell recording while `VoiceControlPanel` has parallel recording logic | Test both paths, then consolidate only after behavior is proven |
| Logs switches | Top `Mostrar logs` works; Admin tab `Registrar logs en avanzado` appears unwired | Remove, hide, or wire intentionally during cleanup phase |
| RF3 chat vs StreamAdmin authenticated chat | Both can connect chat and affect related state | Rename/copy clearly: public RF3 ingest vs authenticated admin chat |
| Twitch button | Disabled placeholder | Keep only if marked `Próximamente`, otherwise hide |
| Storage UI | Storage config exists but no visible UI control found | Add under System only after storage behavior is stable |

## Initial classification decisions

- Stream Admin stays, but moves into the **Stream** product category.
- Model + profile + memory become **Agent / Brain**.
- TTS + recording + WAV + mic + LiveAudio + PTT become **Voice / Input**.
- Kira's response/state/avatar become the persistent **left product panel**.
- Logs become advanced and hidden by default.
- Avatar/OBS starts as a real module boundary with placeholder/config, not a one-off UI button.
