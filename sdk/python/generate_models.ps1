param([switch]$Check)
$ErrorActionPreference = 'Stop'
$arguments = @((Join-Path $PSScriptRoot 'generate_models.py'))
if ($Check) { $arguments += '--check' }
python @arguments
if ($LASTEXITCODE -ne 0) { throw "model generation failed with exit code $LASTEXITCODE" }
