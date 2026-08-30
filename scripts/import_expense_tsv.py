import argparse
import csv
import datetime as dt
import os
from decimal import Decimal

import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_batch


def parse_args():
    parser = argparse.ArgumentParser(description="Import expense TSV into transactions table")
    parser.add_argument("--tsv", default="支出データ.tsv", help="Path to TSV file")
    parser.add_argument("--db-host", default=os.getenv("DB_HOST"))
    parser.add_argument("--db-port", default=os.getenv("DB_PORT", "5432"))
    parser.add_argument("--db-name", default=os.getenv("DB_NAME"))
    parser.add_argument("--db-user", default=os.getenv("DB_USER"))
    parser.add_argument("--db-password", default=os.getenv("DB_PASSWORD"))
    parser.add_argument("--db-schema", default=os.getenv("DB_SCHEMA", "dwh"))
    parser.add_argument("--db-sslmode", default=os.getenv("DB_SSLMODE", "require"))
    return parser.parse_args()


def parse_date(value: str) -> dt.date:
    value = (value or "").strip()
    if len(value) == 8 and value.isdigit():
        return dt.datetime.strptime(value, "%Y%m%d").date()
    return dt.datetime.strptime(value, "%Y-%m-%d").date()


def map_entry_type(value: str) -> str:
    raw = (value or "").strip()
    mapping = {
        "支": "expense",
        "収": "income",
        "expense": "expense",
        "income": "income",
    }
    if raw not in mapping:
        raise ValueError(f"Unsupported 収支区分: {raw}")
    return mapping[raw]


def normalize_category_name(name: str) -> str:
    raw = (name or "").strip()
    mapping = {
        "特別交際費": "恋活",
        "ビジネス": "自己投資",
        "引っ越し": "固定費",
        "引越し": "固定費",
        "趣味": "お小遣い",
    }
    return mapping.get(raw, raw)


def ensure_account(cur, account_name: str) -> int:
    cur.execute("SELECT account_id FROM m_account WHERE account_name = %s", (account_name,))
    row = cur.fetchone()
    if row:
        return row[0]

    cur.execute(
        "INSERT INTO m_account (account_name, created_at, updated_at) VALUES (%s, now(), now()) RETURNING account_id",
        (account_name,),
    )
    return cur.fetchone()[0]


def resolve_account_name_from_tag(tag_value: str) -> str:
    raw = (tag_value or "").strip()
    if raw == "":
        return "クレジット"
    if raw == "現金":
        return "現金"
    if raw == "クレジット":
        return "クレジット"
    return "クレジット"


def get_or_create_category(cur, cache: dict, category_name: str) -> int:
    if category_name in cache:
        return cache[category_name]

    cur.execute("SELECT category_id FROM m_category WHERE category_name = %s", (category_name,))
    row = cur.fetchone()
    if row:
        cache[category_name] = row[0]
        return row[0]

    cur.execute(
        "INSERT INTO m_category (category_name, created_at, updated_at) VALUES (%s, now(), now()) RETURNING category_id",
        (category_name,),
    )
    category_id = cur.fetchone()[0]
    cache[category_name] = category_id
    return category_id


def main():
    args = parse_args()

    required = {
        "db_host": args.db_host,
        "db_name": args.db_name,
        "db_user": args.db_user,
        "db_password": args.db_password,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise SystemExit(f"Missing DB settings: {', '.join(missing)}")

    with open(args.tsv, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        rows = list(reader)

    conn = psycopg2.connect(
        host=args.db_host,
        port=args.db_port,
        dbname=args.db_name,
        user=args.db_user,
        password=args.db_password,
        sslmode=args.db_sslmode,
    )

    inserted_count = 0
    created_categories = 0

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(args.db_schema)))

                account_id_cache = {
                    "現金": ensure_account(cur, "現金"),
                    "クレジット": ensure_account(cur, "クレジット"),
                }

                category_cache = {}
                insert_values = []

                for row in rows:
                    category_name = normalize_category_name(row.get("カテゴリ"))
                    amount_raw = (row.get("金額") or "").strip()
                    date_raw = (row.get("日付") or "").strip()
                    entry_type_raw = row.get("収支区分")
                    tag_raw = (row.get("タグ") or "").strip()
                    memo_raw = (row.get("メモ") or "").strip()

                    if not category_name or not amount_raw or not date_raw:
                        continue

                    category_id_before = category_cache.get(category_name)
                    category_id = get_or_create_category(cur, category_cache, category_name)
                    if category_id_before is None:
                        cur.execute("SELECT 1 FROM m_category WHERE category_id = %s", (category_id,))
                        if cur.fetchone():
                            created_categories += 1

                    tx_date = parse_date(date_raw)
                    amount = Decimal(amount_raw)
                    entry_type = map_entry_type(entry_type_raw)
                    account_name = resolve_account_name_from_tag(tag_raw)
                    account_id = account_id_cache[account_name]
                    memo = memo_raw

                    insert_values.append(
                        (tx_date, category_id, amount, account_id, entry_type, memo if memo else None)
                    )

                execute_batch(
                    cur,
                    """
                    INSERT INTO t_transaction (
                        transaction_date, category_id, amount, account_id,
                        entry_type, memo, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, now(), now())
                    """,
                    insert_values,
                    page_size=500,
                )
                inserted_count = len(insert_values)

        print(f"Imported rows: {inserted_count}")
        print("Accounts used: 現金 / クレジット (空白タグはクレジット)")
        print(f"Categories touched: {len(set((r.get('カテゴリ') or '').strip() for r in rows if (r.get('カテゴリ') or '').strip()))}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()