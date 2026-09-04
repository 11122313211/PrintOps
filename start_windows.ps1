$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$port = $env:PRINTOPS_PORT
if ([string]::IsNullOrWhiteSpace($port)) {
    $port = "4174"
}

$python = $env:PRINTOPS_PYTHON
if ([string]::IsNullOrWhiteSpace($python)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $python = "py"
    } else {
        $python = "python"
    }
}

Write-Host "PrintOps is starting at http://localhost:$port"
& $python server.py
