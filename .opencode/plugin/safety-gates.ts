import type { Plugin } from "@opencode-ai/plugin"

const DANGEROUS_GIT_PATTERNS = [
  /(?:^|[;&|\n])\s*git\s+add\s+(?:\.|-A|--all)(?:\s|$)/i,
  /(?:^|[;&|\n])\s*git\s+commit\s+-am(?:\s|$)/i,
  /(?:^|[;&|\n])\s*git\s+clean(?:\s|$)/i,
  /(?:^|[;&|\n])\s*git\s+reset\s+--hard(?:\s|$)/i,
]

export default (async () => {
  return {
    "tool.execute.before": async (_input, output) => {
      const command = String(output.args?.command ?? "")
      if (!command) return

      const blocked = DANGEROUS_GIT_PATTERNS.some((pattern) => pattern.test(command))
      if (!blocked) return

      throw new Error(
        [
          "Blocked by VoiceAI safety gate.",
          "Do not use broad/destructive Git commands from OpenCode.",
          "Run `/safe-stage-check` or `python tools/safe_stage_check.py` first, then stage explicit paths only.",
        ].join(" "),
      )
    },
  }
}) satisfies Plugin
