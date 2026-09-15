[CmdletBinding()]
param()

# Thin wrapper kept for existing Windows shortcuts. The bundle is built by the
# cross-platform Python script so that Codex/web derivatives always come from
# the single source `.claude/agents/*.md`.
$ErrorActionPreference = 'Stop'
$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = if (Test-Path (Join-Path $scriptDirectory '..\.venv\Scripts\python.exe')) { Join-Path $scriptDirectory '..\.venv\Scripts\python.exe' } else { 'python' }
& $python (Join-Path $scriptDirectory 'build_role_derivatives.py') --web
exit $LASTEXITCODE
