param(
    [switch]$StagedOnly = $true
)

$ErrorActionPreference = "Stop"

function Write-Block($message) {
    Write-Host "[git-safety] $message" -ForegroundColor Red
}

function Write-Info($message) {
    Write-Host "[git-safety] $message" -ForegroundColor Cyan
}

$staged = git diff --cached --name-only --diff-filter=ACMR
if (-not $staged) {
    Write-Info "No staged files to check."
    exit 0
}

$blockedPathPatterns = @(
    '(^|/)\.env$',
    '(^|/).*\.env$',
    '^data/',
    '^modelos_f5/',
    '^temp/',
    '^logs/',
    '^\.coverage$',
    '(^|/)\.pytest_cache/',
    '\.(db|sqlite|sqlite3|jsonl|log|wav|mp3|mp4|pth|pt|safetensors|bin)$',
    'oauth_(client|tokens)\.json$',
    '(^|/)tokens?/'
)

$blocked = @()
foreach ($path in $staged) {
    $normalized = $path -replace '\\', '/'
    foreach ($pattern in $blockedPathPatterns) {
        if ($normalized -match $pattern) {
            $blocked += $path
            break
        }
    }
}

if ($blocked.Count -gt 0) {
    Write-Block "Blocked potentially private/runtime artifacts:"
    $blocked | Sort-Object -Unique | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    Write-Host ""
    Write-Block "If this is intentional, review manually and use 'git commit --no-verify'."
    exit 1
}

$secretPatterns = @(
    '(?i)(api[_-]?key|secret|password|passwd|token|client_secret)\s*[:=]\s*[''"]?[A-Za-z0-9_\-\.]{16,}',
    '(?i)authorization\s*[:=]\s*[''"]?bearer\s+[A-Za-z0-9_\-\.]{16,}',
    '(?i)hf_[A-Za-z0-9]{20,}',
    '(?i)sk-[A-Za-z0-9]{20,}'
)

$secretHits = @()
foreach ($path in $staged) {
    $normalized = $path -replace '\\', '/'
    if ($normalized -match '\.(png|jpg|jpeg|gif|webp|ico|pdf|zip|7z|gz|tar|wav|mp3|mp4|pth|pt|safetensors|bin)$') {
        continue
    }

    $content = git show ":$path" 2>$null
    if ($LASTEXITCODE -ne 0 -or $null -eq $content) {
        continue
    }

    $lineNo = 0
    foreach ($line in $content) {
        $lineNo++
        foreach ($pattern in $secretPatterns) {
            if ($line -match $pattern) {
                $secretHits += "${path}:${lineNo}"
                break
            }
        }
    }
}

if ($secretHits.Count -gt 0) {
    Write-Block "Possible secrets found in staged content:"
    $secretHits | Sort-Object -Unique | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    Write-Host ""
    Write-Block "Remove/redact the secret before committing, or bypass only after manual review."
    exit 1
}

Write-Info "Passed."
exit 0
