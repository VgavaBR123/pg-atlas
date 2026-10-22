# Prepara a pasta de trabalho (workspace) do pg-atlas.
#
# Uso, a partir da pasta de trabalho (a que contém pg-atlas e o repositório de documentação):
#   powershell -ExecutionPolicy Bypass -File .\pg-atlas\scripts\preparar-workspace.ps1
#   powershell -ExecutionPolicy Bypass -File .\pg-atlas\scripts\preparar-workspace.ps1 -Doc outro-repo
#
# O que faz:
#   - copia CLAUDE.md, .claude/ e .env.example de pg-atlas/workspace/ para a pasta de trabalho;
#   - troca o nome do repositório de documentação no CLAUDE.md, se for diferente de atlas-eldados;
#   - nunca cria, lê nem sobrescreve o arquivo .env.

param(
    [string]$Doc = "atlas-eldados"
)

$ErrorActionPreference = "Stop"

$origem = Join-Path $PSScriptRoot "..\workspace" | Resolve-Path
$destino = Join-Path $PSScriptRoot "..\.." | Resolve-Path

if (-not (Test-Path (Join-Path $destino $Doc))) {
    throw "Não encontrei a pasta '$Doc' em $destino. Clone o repositório de documentação ao lado do pg-atlas."
}

Copy-Item (Join-Path $origem "CLAUDE.md") $destino -Force
Copy-Item (Join-Path $origem ".env.example") $destino -Force
$claudeDestino = Join-Path $destino ".claude"
New-Item -ItemType Directory -Force -Path $claudeDestino | Out-Null
Copy-Item (Join-Path $origem ".claude\*") $claudeDestino -Recurse -Force

if ($Doc -ne "atlas-eldados") {
    $arquivo = Join-Path $destino "CLAUDE.md"
    (Get-Content $arquivo -Raw -Encoding UTF8).Replace("atlas-eldados", $Doc) |
        Set-Content $arquivo -Encoding UTF8
}

Write-Host "Workspace preparado em $destino"
if (-not (Test-Path (Join-Path $destino ".env"))) {
    Write-Host "Falta o arquivo .env: copie .env.example para .env e preencha os dados de conexão."
}
