param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("up", "down", "logs", "inventory", "backup", "restore")]
    [string]$Action,
    [ValidateSet("core", "semantic", "governance")]
    [string]$Profile = "core",
    [string]$Path = ""
)

$Compose = @("compose", "-p", "kr-v2", "-f", "deploy/kr-v2.compose.yml")

switch ($Action) {
    "up" { docker @Compose --profile $Profile up -d }
    "down" { docker @Compose down }
    "logs" { docker @Compose logs --tail 200 }
    "inventory" { docker @Compose ps }
    "backup" {
        if (-not $Path) { throw "backup requires -Path" }
        docker @Compose exec -T postgres pg_dump -U kr knowledge_runtime | Out-File -FilePath $Path -Encoding utf8
    }
    "restore" {
        if (-not $Path) { throw "restore requires -Path" }
        Get-Content -Raw -LiteralPath $Path | docker @Compose exec -T postgres psql -U kr knowledge_runtime
    }
}

