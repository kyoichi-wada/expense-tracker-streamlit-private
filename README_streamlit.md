# ExpenseTracker Streamlit

PC 上で操作する家計簿 Web アプリです。

- 記帳（transactions へ直接 INSERT）
- 過去明細の表示
- デフォルト表示は当月、日付昇順

## 1. セットアップ

PowerShell:

1. `Set-Location C:/dev/AzureFunctions/ExpenseTrackerFunc`
2. `python -m pip install -r requirements_streamlit.txt`

## 2. 環境変数設定（同じ DB を利用）

PowerShell:

1. `$env:DB_HOST='<your-db-host>'`
2. `$env:DB_PORT='5432'`
3. `$env:DB_NAME='<your-db-name>'`
4. `$env:DB_USER='<your-db-user>'`
5. `$env:DB_PASSWORD='<your-db-password>'`
6. `$env:DB_SCHEMA='dwh'`
7. `$env:DB_SSLMODE='require'`

Streamlit Community Cloud では、上記キーを Secrets に設定してください。

## 3. 起動

PowerShell:

1. `streamlit run streamlit_app.py --server.address 127.0.0.1 --server.port 8501`

ブラウザ:

- http://127.0.0.1:8501