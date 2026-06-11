# SDD / Skills Usage

VoiceAI has Conductor SDD skills installed in:

```text
.agents/skills/
```

## Required Memory Step

Before any SDD work, recover project memory from Engram:

```text
Use Engram memory for project voiceai. First call mem_context, then mem_search if needed, and treat the recovered memories as constraints.
```

This is required because VoiceAI already has important project memory about offline TTS, cached Qwen3-TTS startup, local Hugging Face model resolution, and heavy TTS timeouts.

## Available Conductor Skills

- `conductor-setup`: initialize the Conductor project structure.
- `conductor-newTrack`: create a new feature/bugfix/refactor track.
- `conductor-implement`: implement a selected track after the spec and tasks are clear.
- `conductor-status`: inspect current tracks and progress.
- `conductor-review`: review a track before closing it.
- `conductor-revert`: revert a track only when explicitly requested.

## Existing VoiceAI Skill

- `vocalai-ui-ux-architect`

## Recommended Flow

For a new feature or ambiguous change:

```text
Recover Engram context for voiceai, then use conductor-newTrack to create the spec, plan, tasks, risks, and acceptance criteria before coding.
```

Then:

```text
Use conductor-implement to implement the approved track one task at a time.
```

Before closing:

```text
Use conductor-review and any relevant VoiceAI-specific skill to verify the implementation against the spec and previous project memory.
```

After meaningful work:

```text
Save the final decisions, discoveries, and completed track summary in Engram with mem_save or mem_session_summary.
```

## Terminal Engram Examples

If using the Engram CLI directly (path depends on your installation), the commands follow this pattern:

```powershell
engram context voiceai
engram search "Qwen3-TTS offline" --project voiceai
engram save "OpenCohost decision" "Decision/details here." --type decision --project voiceai
```

Set `ENGRAM_EXE` in your environment if the `engram` binary is not on your `PATH`.
