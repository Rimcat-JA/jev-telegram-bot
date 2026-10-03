$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    python -X utf8 bot.py
    $botExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $botExitCode
