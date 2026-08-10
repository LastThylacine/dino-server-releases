param(
    [Parameter(Mandatory = $true)]
    [string]$ProgramPath
)

$ErrorActionPreference = "Stop"
$group = "Dino Server"
$oldNames = @(
    "JPB Local Server",
    "Dinosaur Game Private Server",
    "Dino Server one-click local server"
)

foreach ($name in $oldNames) {
    Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule -ErrorAction SilentlyContinue
}
Get-NetFirewallRule -Group $group -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule -ErrorAction SilentlyContinue

New-NetFirewallRule `
    -DisplayName "Dino Server (TCP)" `
    -Group $group `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalPort 53,80,9933,9943 `
    -Profile Private,Public `
    -RemoteAddress LocalSubnet `
    -Program $ProgramPath | Out-Null

New-NetFirewallRule `
    -DisplayName "Dino Server (DNS UDP)" `
    -Group $group `
    -Direction Inbound `
    -Action Allow `
    -Protocol UDP `
    -LocalPort 53 `
    -Profile Private,Public `
    -RemoteAddress LocalSubnet `
    -Program $ProgramPath | Out-Null

exit 0
