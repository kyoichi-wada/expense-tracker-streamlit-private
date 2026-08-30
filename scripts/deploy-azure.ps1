param(
  [Parameter(Mandatory = $true)]
  [string]$SubscriptionId,

  [string]$ResourceGroup = "rg-expensetracker-dev",
  [string]$Location = "japaneast",

  [string]$FunctionAppName = "expensetrackerfunc",
  [string]$StorageAccountName = "",

  [Parameter(Mandatory = $true)]
  [string]$DbHost,

  [string]$DbPort = "5432",

  [Parameter(Mandatory = $true)]
  [string]$DbName,

  [Parameter(Mandatory = $true)]
  [string]$DbUser,

  [Parameter(Mandatory = $true)]
  [string]$DbPassword,

  [string]$DbSchema = "dwh",
  [string]$DbSslMode = "require",
  [string]$CreatedBy = "ExpenseTrackerFunc",
  [string]$UpdatedBy = "ExpenseTrackerFunc"
)

$ErrorActionPreference = "Stop"

function Require-Command {
  param([string]$Name)
  if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
    throw "Command not found: $Name"
  }
}

function New-StorageAccountName {
  param([string]$BaseName)
  $normalized = ($BaseName.ToLower() -replace "[^a-z0-9]", "")
  if ($normalized.Length -gt 18) {
    $normalized = $normalized.Substring(0, 18)
  }
  $suffix = -join ((48..57) + (97..122) | Get-Random -Count 6 | ForEach-Object {[char]$_})
  return ($normalized + $suffix)
}

Write-Host "[1/8] Checking required commands..." -ForegroundColor Cyan
Require-Command -Name "az"
Require-Command -Name "func"
Require-Command -Name "python"

Write-Host "[2/8] Checking Azure login and selecting subscription..." -ForegroundColor Cyan
$null = az account show 2>$null
if ($LASTEXITCODE -ne 0) {
  throw "Azure CLI is not logged in. Run 'az login' first."
}
az account set --subscription $SubscriptionId | Out-Null

if ([string]::IsNullOrWhiteSpace($StorageAccountName)) {
  $StorageAccountName = New-StorageAccountName -BaseName $FunctionAppName
}

if ($StorageAccountName.Length -lt 3 -or $StorageAccountName.Length -gt 24) {
  throw "StorageAccountName must be 3-24 characters."
}

Write-Host "[3/8] Creating resource group if needed..." -ForegroundColor Cyan
az group create --name $ResourceGroup --location $Location | Out-Null

Write-Host "[4/8] Creating storage account if needed..." -ForegroundColor Cyan
$storageExists = az storage account check-name --name $StorageAccountName --query nameAvailable -o tsv
if ($storageExists -eq "true") {
  az storage account create `
    --name $StorageAccountName `
    --resource-group $ResourceGroup `
    --location $Location `
    --sku Standard_LRS `
    --kind StorageV2 | Out-Null
}

Write-Host "[5/8] Creating Function App if needed..." -ForegroundColor Cyan
$appExists = az functionapp list --resource-group $ResourceGroup --query "[?name=='$FunctionAppName'] | length(@)" -o tsv
if ($appExists -eq "0") {
  az functionapp create `
    --name $FunctionAppName `
    --resource-group $ResourceGroup `
    --storage-account $StorageAccountName `
    --consumption-plan-location $Location `
    --runtime python `
    --runtime-version 3.11 `
    --functions-version 4 `
    --os-type Linux | Out-Null
}

Write-Host "[6/8] Applying application settings..." -ForegroundColor Cyan
$settings = @(
  "FUNCTIONS_WORKER_RUNTIME=python",
  "DB_HOST=$DbHost",
  "DB_PORT=$DbPort",
  "DB_NAME=$DbName",
  "DB_USER=$DbUser",
  "DB_PASSWORD=$DbPassword",
  "DB_SCHEMA=$DbSchema",
  "DB_SSLMODE=$DbSslMode",
  "CREATED_BY=$CreatedBy",
  "UPDATED_BY=$UpdatedBy"
)

az functionapp config appsettings set `
  --resource-group $ResourceGroup `
  --name $FunctionAppName `
  --settings $settings | Out-Null

Write-Host "[7/8] Installing Python dependencies..." -ForegroundColor Cyan
python -m pip install -r requirements.txt

Write-Host "[8/8] Publishing Function App..." -ForegroundColor Cyan
func azure functionapp publish $FunctionAppName --python

$hostName = az functionapp show --resource-group $ResourceGroup --name $FunctionAppName --query defaultHostName -o tsv

Write-Host "Deployment completed." -ForegroundColor Green
Write-Host "Function base URL: https://$hostName/api" -ForegroundColor Green
Write-Host "- GET https://$hostName/api/expense/categories" -ForegroundColor Green
Write-Host "- GET https://$hostName/api/expense/accounts" -ForegroundColor Green
Write-Host "- POST https://$hostName/api/expense/transactions" -ForegroundColor Green