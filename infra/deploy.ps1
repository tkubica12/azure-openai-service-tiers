<#
.SYNOPSIS
  Deploys the whole demo: infrastructure (Bicep), worker image (ACR build) and container apps.
.EXAMPLE
  ./infra/deploy.ps1                      # full deployment
  ./infra/deploy.ps1 -SkipImage           # re-deploy infra/apps with the existing image tag ('latest' unless -ImageTag is given)
#>
param(
    [string]$ResourceGroup = 'rg-openai-flex-demo',
    [string]$Location = 'swedencentral',
    [string]$ImageTag = (Get-Date -Format 'yyyyMMddHHmmss'),
    [switch]$SkipImage,
    [switch]$InfraOnly,
    [string]$ProbeCron = '*/15 * * * *'   # '' removes nothing but skips deploying the probe job
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent

$myIp = (Invoke-RestMethod https://api.ipify.org).Trim()
$operatorId = az ad signed-in-user show --query id -o tsv
Write-Host "Operator $operatorId, laptop IP $myIp"

az group create -n $ResourceGroup -l $Location --tags project=openai-flex-processing SecurityControl=Ignore -o none

# Keep IPs added earlier (e.g. by allow-my-ip.ps1) - corporate egress IPs tend to rotate.
$existingIps = @(az storage account list -g $ResourceGroup --query "[0].networkRuleSet.ipRules[].ipAddressOrRange" -o tsv 2>$null)
$allowedIps = @(@($myIp) + $existingIps | Where-Object { $_ } | Sort-Object -Unique)
Write-Host "Storage firewall IPs: $($allowedIps -join ', ')"

function Deploy([bool]$apps, [string]$tag) {
    $params = @{
        allowedIps          = @{ value = $allowedIps }
        operatorPrincipalId = @{ value = $operatorId }
        deployApps          = @{ value = $apps }
        imageTag            = @{ value = $tag }
        probeCron           = @{ value = $ProbeCron }
    }
    $pfile = Join-Path $env:TEMP "flexdemo-params.json"
    @{ '$schema' = 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#'; contentVersion = '1.0.0.0'; parameters = $params } |
        ConvertTo-Json -Depth 10 | Set-Content $pfile
    az deployment group create -g $ResourceGroup -n "flexdemo-$(Get-Date -Format 'MMddHHmmss')" `
        -f (Join-Path $PSScriptRoot 'main.bicep') -p "@$pfile" --query properties.outputs -o json | Tee-Object -Variable raw | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Bicep deployment failed" }
    ($raw -join "`n") | ConvertFrom-Json
}

$out = Deploy $false 'latest'
$out.PSObject.Properties | ForEach-Object { [pscustomobject]@{ name = $_.Name; value = $_.Value.value } } |
    ConvertTo-Json | Set-Content (Join-Path $root 'outputs.json')
if ($InfraOnly) { return }

if (-not $SkipImage) {
    az acr build -r $out.acrName.value -t "flex-worker:$ImageTag" -t 'flex-worker:latest' (Join-Path $root 'worker') --no-logs -o none
    if ($LASTEXITCODE -ne 0) { throw 'ACR build failed' }
} elseif (-not $PSBoundParameters.ContainsKey('ImageTag')) {
    $ImageTag = 'latest'
}
$null = Deploy $true $ImageTag
Write-Host "Deployed apps with image tag $ImageTag"

