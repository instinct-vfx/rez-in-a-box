# SPDX-License-Identifier: Apache-2.0
# Shim: make sure uv is available, then hand over to rezbox.py.
$ErrorActionPreference = 'Stop'

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    $localUv = Join-Path $HOME '.local\bin'
    if (Test-Path (Join-Path $localUv 'uv.exe')) {
        $env:PATH = "$localUv;$env:PATH"
    } else {
        Write-Host '[rezbox] uv not found, installing it (https://docs.astral.sh/uv/)'
        powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
        $env:PATH = "$localUv;$env:PATH"
    }
}

& uv run --quiet --script (Join-Path $PSScriptRoot 'rezbox.py') @args
exit $LASTEXITCODE
