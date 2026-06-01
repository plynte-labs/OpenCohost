# VoiceAI — OpenCode Agent Delegation Script
# Usage:
#   .\scripts\delegate.ps1 run "Revisá core/llm_engine.py"
#   .\scripts\delegate.ps1 review "src/core/"
#   .\scripts\delegate.ps1 feature "Agregá cancelación de TTS"
#   .\scripts\delegate.ps1 session-start

param(
    [Parameter(Mandatory=$true)]
    [string]$Action,

    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$Arguments
)

$ErrorActionPreference = "Stop"
$ProjectDir = $PSScriptRoot | Split-Path -Parent
$AgentName = "voiceai"

# Colores bonitos
function Write-Step { param($Text) Write-Host "`n>> $Text" -ForegroundColor Cyan }
function Write-OK { param($Text) Write-Host "   $Text" -ForegroundColor Green }
function Write-Warn { param($Text) Write-Host "   $Text" -ForegroundColor Yellow }

# Verificar que opencode existe
$opencode = Get-Command opencode -ErrorAction SilentlyContinue
if (-not $opencode) {
    Write-Host "ERROR: opencode no está instalado o no está en el PATH." -ForegroundColor Red
    Write-Host "Instalalo desde: https://opencode.ai/docs/" -ForegroundColor Yellow
    exit 1
}

Write-Step "OpenCode detectado: $(opencode --version 2>$null)"

switch ($Action) {
    "run" {
        $prompt = $Arguments -join " "
        if (-not $prompt) { Write-Warn "Sin prompt. Uso: delegate.ps1 run 'tu prompt'"; exit 1 }
        Write-Step "Delegando a voiceai..."
        opencode run --agent $AgentName --dir $ProjectDir $prompt
    }
    "review" {
        $target = if ($Arguments) { $Arguments -join " " } else { "src/" }
        Write-Step "Delegando revisión de '$target'..."
        opencode run --agent $AgentName --dir $ProjectDir "Revisá el código en $target. Reportá hallazgos en JSON con file, line, severity (CRÍTICO/WARNING/SUGERENCIA), skill, finding, fix_suggestion. NO modifiques archivos."
    }
    "feature" {
        $desc = $Arguments -join " "
        if (-not $desc) { Write-Warn "Describí la feature. Uso: delegate.ps1 feature 'descripción'"; exit 1 }
        Write-Step "Delegando scaffolding de feature..."
        opencode run --agent $AgentName --dir $ProjectDir "Creá una feature nueva: $desc. Seguí stress-first TDD: tests rojos primero, implementación mínima, intentá romper tus tests. No commitear."
    }
    "session-start" {
        Write-Step "Iniciando sesión VoiceAI..."
        opencode run --command session-start --dir $ProjectDir
    }
    "safe-stage" {
        Write-Step "Safe stage check..."
        opencode run --command safe-stage-check --dir $ProjectDir
    }
    default {
        Write-Host "Acción no reconocida: $Action" -ForegroundColor Red
        Write-Host "Uso: delegate.ps1 [run|review|feature|session-start|safe-stage] [args...]" -ForegroundColor Yellow
        exit 1
    }
}

Write-OK "Listo."
