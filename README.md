# ExpenseTrackerFunc (Azure Functions)

Python Azure Functions v2 model + PostgreSQL で家計簿取引 API を提供します。

## API

- GET `/api/expense/categories`
- GET `/api/expense/accounts`
- POST `/api/expense/transactions`

## ローカル実行

1. `local.settings.json.example` を `local.settings.json` として作成
2. DB 接続情報を設定
3. 依存をインストール
4. Function を起動

```powershell
Set-Location C:/dev/AzureFunctions/ExpenseTrackerFunc
Copy-Item local.settings.json.example local.settings.json
python -m pip install -r requirements.txt
func start
```

## Azure にリソース作成からデプロイまで一括実行

### 事前条件

- Azure CLI (`az`)
- Azure Functions Core Tools (`func`)
- Python 3.11+
- Azure ログイン済み (`az login`)

### 実行

```powershell
Set-Location C:/dev/AzureFunctions/ExpenseTrackerFunc
./scripts/deploy-azure.ps1 \
  -SubscriptionId "<your-subscription-id>" \
  -ResourceGroup "rg-expensetracker-dev" \
  -Location "japaneast" \
  -FunctionAppName "expensetrackerfunc" \
  -DbHost "<your-db-host>" \
  -DbName "<your-db-name>" \
  -DbUser "<your-db-user>" \
  -DbPassword "<your-db-password>" \
  -DbSchema "dwh"
```

このスクリプトは以下を実施します。

1. Resource Group 作成
2. Storage Account 作成（未指定時は自動命名）
3. Function App 作成（Linux Consumption, Python 3.11）
4. DB 系 App Settings 設定
5. `func azure functionapp publish` によるデプロイ

## POST サンプル

```json
{
  "transaction_date": "2026-07-26",
  "category_id": 1,
  "amount": 1200,
  "account_id": 1,
  "entry_type": "expense",
  "memo": "ランチ"
}
```