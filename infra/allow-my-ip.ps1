<#
.SYNOPSIS
  Adds an IP/CIDR to the storage account firewall (the storage account is otherwise reachable only
  through its private endpoint from the Container Apps VNet).
.NOTES
  Behind a corporate secure-web-gateway the source IP seen by Azure Storage may differ from what
  api.ipify.org reports. Look it up in the blob logs (StorageBlobLogs.CallerIpAddress in the Log
  Analytics workspace) and pass it with -Ip.
#>
param(
    [string]$ResourceGroup = 'rg-openai-flex-demo',
    [string]$Ip = ''
)
$ErrorActionPreference = 'Stop'
if (-not $Ip) { $Ip = (Invoke-RestMethod https://api.ipify.org).Trim() }
$sa = az storage account list -g $ResourceGroup --query "[0].name" -o tsv
az storage account network-rule add -g $ResourceGroup --account-name $sa --ip-address $Ip -o none
Write-Host "Added $Ip to $sa firewall (can take a minute or two to apply)"
