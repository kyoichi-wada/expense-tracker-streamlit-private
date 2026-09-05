import datetime
import json
import logging
import os
import re
from contextlib import contextmanager
from zoneinfo import ZoneInfo

import azure.functions as func
import psycopg2
from psycopg2 import pool, sql

# ---- DB settings ----
DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "port": os.getenv("DB_PORT", "5432"),
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "sslmode": os.getenv("DB_SSLMODE", "require"),
}
DB_SCHEMA = os.getenv("DB_SCHEMA", "dwh")
CREATED_BY = os.getenv("CREATED_BY", "ExpenseTrackerFunc")
UPDATED_BY = os.getenv("UPDATED_BY", "ExpenseTrackerFunc")

MONTHLY_FIXED_EXPENSE_SCHEDULE = os.getenv("MONTHLY_FIXED_EXPENSE_SCHEDULE", "0 0 15 28-31 * *")
MONTHLY_FIXED_EXPENSE_TZ = os.getenv("MONTHLY_FIXED_EXPENSE_TZ", "Asia/Tokyo")
MONTHLY_FIXED_EXPENSE_ACCOUNT = os.getenv("MONTHLY_FIXED_EXPENSE_ACCOUNT", "クレジット")
MONTHLY_FIXED_EXPENSE_ITEMS = os.getenv(
    "MONTHLY_FIXED_EXPENSE_ITEMS",
    '[{"category":"固定費","amount":250000,"account":"現金","memo":"固定費"},'
    '{"category":"食費","amount":20000,"account":"クレジット","memo":"平日昼食の固定食費"},'
    '{"category":"日常費","amount":400,"account":"現金","memo":"日本通信"}]',
)

_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, **DB_CONFIG)
    return _pool


@contextmanager
def get_conn():
    p = _get_pool()
    conn = p.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(DB_SCHEMA)))
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        p.putconn(conn)


app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


def _parse_monthly_items_relaxed(raw: str):
    # Accept relaxed object style like:
    # [{category:固定費,amount:250000,account:現金,memo:固定費}, ...]
    text = (raw or "").strip()
    if not text:
        return []

    text = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)", r'\1"\2"\3', text)

    def quote_bare_value(match):
        token = match.group(1).strip()
        if token.startswith('"') or token.startswith('{') or token.startswith('['):
            return ": " + token
        if re.fullmatch(r"-?\d+(\.\d+)?", token):
            return ": " + token
        if token in {"true", "false", "null"}:
            return ": " + token
        escaped = token.replace('\\', '\\\\').replace('"', '\\"')
        return ': "' + escaped + '"'

    text = re.sub(r":\s*([^,}\]]+)", quote_bare_value, text)
    return json.loads(text)


def load_monthly_fixed_items():
    try:
        items = json.loads(MONTHLY_FIXED_EXPENSE_ITEMS)
    except json.JSONDecodeError as e:
        try:
            items = _parse_monthly_items_relaxed(MONTHLY_FIXED_EXPENSE_ITEMS)
        except Exception as e2:
            raise ValueError(f"MONTHLY_FIXED_EXPENSE_ITEMS is invalid JSON: {e}") from e2

    if not isinstance(items, list):
        raise ValueError("MONTHLY_FIXED_EXPENSE_ITEMS must be a JSON array")

    normalized = []
    for i, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Item {i} must be object")

        category = str(item.get("category", "")).strip()
        amount = item.get("amount")
        memo = str(item.get("memo", "")).strip()
        account = str(item.get("account", MONTHLY_FIXED_EXPENSE_ACCOUNT)).strip()

        if not category:
            raise ValueError(f"Item {i} missing category")
        if amount is None:
            raise ValueError(f"Item {i} missing amount")
        if not account:
            raise ValueError(f"Item {i} missing account")

        try:
            amount_val = float(amount)
        except (TypeError, ValueError) as e:
            raise ValueError(f"Item {i} amount is invalid: {amount}") from e

        if amount_val < 0:
            raise ValueError(f"Item {i} amount must be >= 0")

        normalized.append(
            {
                "category": category,
                "amount": amount_val,
                "memo": memo,
                "account": account,
            }
        )

    return normalized


def run_monthly_fixed_expense_insert(target_date: datetime.date, items: list[dict]):
    inserted = 0
    skipped = 0

    with get_conn() as conn:
        with conn.cursor() as cur:
            account_id_cache: dict[str, int] = {}

            def resolve_account_id(account_name: str):
                if account_name in account_id_cache:
                    return account_id_cache[account_name]
                cur.execute(
                    "SELECT account_id FROM m_account WHERE account_name=%s",
                    (account_name,),
                )
                account_row = cur.fetchone()
                if not account_row:
                    return None
                account_id_cache[account_name] = account_row[0]
                return account_row[0]

            for item in items:
                account_id = resolve_account_id(item["account"])
                if account_id is None:
                    logging.warning("Account not found, skip: %s", item["account"])
                    skipped += 1
                    continue

                cur.execute(
                    "SELECT category_id FROM m_category WHERE category_name=%s",
                    (item["category"],),
                )
                category_row = cur.fetchone()
                if not category_row:
                    logging.warning("Category not found, skip: %s", item["category"])
                    skipped += 1
                    continue
                category_id = category_row[0]

                cur.execute(
                    """
                    SELECT 1
                    FROM t_transaction
                    WHERE transaction_date = %s
                      AND category_id = %s
                      AND amount = %s
                      AND account_id = %s
                      AND entry_type = 'expense'
                      AND COALESCE(memo, '') = %s
                    LIMIT 1
                    """,
                    (
                        target_date,
                        category_id,
                        item["amount"],
                        account_id,
                        item["memo"],
                    ),
                )
                if cur.fetchone():
                    skipped += 1
                    continue

                cur.execute(
                    """
                    INSERT INTO t_transaction (
                        transaction_date, category_id, amount, account_id,
                        entry_type, memo, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, 'expense', %s, now(), now())
                    """,
                    (
                        target_date,
                        category_id,
                        item["amount"],
                        account_id,
                        item["memo"] or None,
                    ),
                )
                inserted += 1

    return {"inserted": inserted, "skipped": skipped}


def parse_target_date(value: str) -> datetime.date:
    return datetime.datetime.strptime(value, "%Y-%m-%d").date()


def parse_bool_param(value: str | None, default: bool) -> bool:
    if value is None:
        return default

    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def month_end(target_date: datetime.date) -> datetime.date:
    if target_date.month == 12:
        next_month = datetime.date(target_date.year + 1, 1, 1)
    else:
        next_month = datetime.date(target_date.year, target_date.month + 1, 1)
    return next_month - datetime.timedelta(days=1)


def get_budget_base_date(include_future: bool) -> datetime.date:
    try:
        tz = ZoneInfo(MONTHLY_FIXED_EXPENSE_TZ)
    except Exception:
        tz = ZoneInfo("Asia/Tokyo")

    today_local = datetime.datetime.now(datetime.timezone.utc).astimezone(tz).date()
    return month_end(today_local) if include_future else today_local


def load_budget_progress(as_of_date: datetime.date):
    query = """
        WITH budget_target AS (
            SELECT
                b.budget_id,
                b.fiscal_year,
                b.annual_budget,
                b.start_month,
                make_date(b.fiscal_year, b.start_month, 1) AS fiscal_start_date
            FROM m_budget b
            WHERE b.fiscal_year = %s
              AND b.start_month = 1
            LIMIT 1
        ),
        as_of_info AS (
            SELECT
                bt.*,
                %s::date AS as_of_date,
                (((EXTRACT(MONTH FROM %s::date)::int - bt.start_month + 12) %% 12) + 1) AS elapsed_months
            FROM budget_target bt
        ),
        numerator_data AS (
            SELECT
                COALESCE(SUM(t.amount), 0) AS expense_amount_excluding_fixed
            FROM as_of_info ai
            LEFT JOIN t_transaction t
                ON t.transaction_date BETWEEN ai.fiscal_start_date AND ai.as_of_date
               AND t.entry_type = 'expense'
               AND NOT EXISTS (
                   SELECT 1
                   FROM m_budget_excluded_category bec
                   WHERE bec.budget_id = ai.budget_id
                     AND bec.category_id = t.category_id
               )
        )
        SELECT
            ai.fiscal_year,
            ai.annual_budget,
            ai.elapsed_months,
            nd.expense_amount_excluding_fixed AS numerator,
            (ai.annual_budget / 12.0 * ai.elapsed_months) AS denominator,
            CASE
                WHEN (ai.annual_budget / 12.0 * ai.elapsed_months) = 0 THEN 0
                ELSE ROUND(nd.expense_amount_excluding_fixed / (ai.annual_budget / 12.0 * ai.elapsed_months) * 100.0, 2)
            END AS progress_rate_percent
        FROM as_of_info ai
        CROSS JOIN numerator_data nd
    """
    fiscal_year = as_of_date.year
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (fiscal_year, as_of_date, as_of_date))
            row = cur.fetchone()
            if not row:
                return None
            return {
                "fiscal_year": row[0],
                "annual_budget": float(row[1]),
                "elapsed_months": int(row[2]),
                "numerator": float(row[3]),
                "denominator": float(row[4]),
                "progress_rate_percent": float(row[5]),
            }


@app.timer_trigger(schedule=MONTHLY_FIXED_EXPENSE_SCHEDULE, arg_name="timer")
def register_monthly_fixed_expenses(timer: func.TimerRequest) -> None:
    try:
        tz = ZoneInfo(MONTHLY_FIXED_EXPENSE_TZ)
    except Exception:
        tz = ZoneInfo("Asia/Tokyo")

    now_jst = datetime.datetime.now(datetime.timezone.utc).astimezone(tz)

    # Trigger only near month boundary in UTC, then enforce exact JST month-start.
    if not (now_jst.day == 1 and now_jst.hour == 0):
        logging.info("Monthly fixed expense skipped. now_jst=%s", now_jst.isoformat())
        return

    target_date = now_jst.date()
    logging.info("Monthly fixed expense started. target_date=%s", target_date.isoformat())

    try:
        items = load_monthly_fixed_items()
    except Exception as e:
        logging.error("Failed to parse MONTHLY_FIXED_EXPENSE_ITEMS: %s", e)
        return

    try:
        result = run_monthly_fixed_expense_insert(target_date=target_date, items=items)

        logging.info(
            "Monthly fixed expense completed. target_date=%s inserted=%s skipped=%s",
            target_date.isoformat(),
            result["inserted"],
            result["skipped"],
        )
    except Exception as e:
        logging.error("Monthly fixed expense failed: %s", e)


@app.route(route="expense/monthly-fixed-expenses/recovery", methods=["POST"])
def run_monthly_fixed_expense_recovery(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("POST /expense/monthly-fixed-expenses/recovery triggered.")

    try:
        body = req.get_json()
    except ValueError:
        body = {}

    target_date_raw = req.params.get("target_date") or body.get("target_date")
    if target_date_raw:
        try:
            target_date = parse_target_date(str(target_date_raw))
        except ValueError:
            return func.HttpResponse(
                json.dumps({"error": "target_date must be YYYY-MM-DD"}, ensure_ascii=False),
                mimetype="application/json",
                status_code=400,
            )
    else:
        tz = ZoneInfo(MONTHLY_FIXED_EXPENSE_TZ)
        target_date = datetime.datetime.now(datetime.timezone.utc).astimezone(tz).date()

    try:
        items = load_monthly_fixed_items()
        result = run_monthly_fixed_expense_insert(target_date=target_date, items=items)
        return func.HttpResponse(
            json.dumps(
                {
                    "message": "リカバリ実行を完了しました",
                    "target_date": target_date.isoformat(),
                    "inserted": result["inserted"],
                    "skipped": result["skipped"],
                    "schedule_guard_ignored": True,
                },
                ensure_ascii=False,
            ),
            mimetype="application/json",
            status_code=200,
        )
    except Exception as e:
        logging.error("Recovery monthly fixed expense failed: %s", e)
        return func.HttpResponse(
            json.dumps({"error": str(e)}, ensure_ascii=False),
            mimetype="application/json",
            status_code=500,
        )


@app.route(route="expense/budget/diff", methods=["GET"])
def get_budget_diff(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("GET /expense/budget/diff triggered.")

    as_of_date_raw = req.params.get("as_of_date")
    include_future = parse_bool_param(req.params.get("include_future"), default=True)
    if as_of_date_raw:
        try:
            as_of_date = parse_target_date(as_of_date_raw)
        except ValueError:
            return func.HttpResponse(
                json.dumps({"error": "as_of_date must be YYYY-MM-DD"}, ensure_ascii=False),
                mimetype="application/json",
                status_code=400,
            )
    else:
            as_of_date = get_budget_base_date(include_future)

    try:
        progress = load_budget_progress(as_of_date)
        if not progress:
            return func.HttpResponse(
                json.dumps(
                    {
                        "error": "該当年の予算マスタが未登録です",
                        "as_of_date": as_of_date.isoformat(),
                    },
                    ensure_ascii=False,
                ),
                mimetype="application/json",
                status_code=404,
            )

        diff = int(round(progress["denominator"] - progress["numerator"]))
        response = {
            "as_of_date": as_of_date.isoformat(),
            "include_future": include_future,
            "fiscal_year": progress["fiscal_year"],
            "annual_budget": progress["annual_budget"],
            "elapsed_months": progress["elapsed_months"],
            "numerator": progress["numerator"],
            "denominator": progress["denominator"],
            "progress_rate_percent": progress["progress_rate_percent"],
            "diff": diff,
            "diff_formatted": f"{diff:,}",
        }
        return func.HttpResponse(
            json.dumps(response, ensure_ascii=False),
            mimetype="application/json",
            status_code=200,
        )
    except Exception as e:
        logging.error("GetBudgetDiff error: %s", e)
        return func.HttpResponse(
            json.dumps({"error": str(e)}, ensure_ascii=False),
            mimetype="application/json",
            status_code=500,
        )


@app.route(route="expense/categories", methods=["GET"])
def get_categories(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("GET /expense/categories triggered.")
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT category_id, category_name "
                    "FROM m_category "
                    "ORDER BY category_id"
                )
                rows = cur.fetchall()
                categories = [f"{r[0]}:{r[1]}" for r in rows]

        return func.HttpResponse(
            json.dumps(categories, ensure_ascii=False),
            mimetype="application/json",
            status_code=200,
        )
    except Exception as e:
        logging.error(f"GetCategories error: {e}")
        return func.HttpResponse(
            json.dumps({"error": str(e)}, ensure_ascii=False),
            mimetype="application/json",
            status_code=500,
        )


@app.route(route="expense/accounts", methods=["GET"])
def get_accounts(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("GET /expense/accounts triggered.")
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT account_id, account_name "
                    "FROM m_account "
                    "ORDER BY account_id"
                )
                rows = cur.fetchall()
                accounts = [f"{r[0]}:{r[1]}" for r in rows]

        return func.HttpResponse(
            json.dumps(accounts, ensure_ascii=False),
            mimetype="application/json",
            status_code=200,
        )
    except Exception as e:
        logging.error(f"GetAccounts error: {e}")
        return func.HttpResponse(
            json.dumps({"error": str(e)}, ensure_ascii=False),
            mimetype="application/json",
            status_code=500,
        )


@app.route(route="expense/transactions", methods=["POST"])
def register_transaction(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("POST /expense/transactions triggered.")

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps({"error": "Invalid JSON"}, ensure_ascii=False),
            mimetype="application/json",
            status_code=400,
        )

    transaction_date = body.get("transaction_date")
    category_id = body.get("category_id")
    amount = body.get("amount")
    account_id = body.get("account_id")
    entry_type = body.get("entry_type")
    memo = body.get("memo")

    if not all([category_id, amount is not None, account_id, entry_type]):
        return func.HttpResponse(
            json.dumps(
                {
                    "error": "category_id, amount, account_id, entry_type は必須です"
                },
                ensure_ascii=False,
            ),
            mimetype="application/json",
            status_code=400,
        )

    if entry_type not in ["income", "expense"]:
        return func.HttpResponse(
            json.dumps(
                {"error": "entry_type は income または expense を指定してください"},
                ensure_ascii=False,
            ),
            mimetype="application/json",
            status_code=400,
        )

    if not transaction_date:
        transaction_date = datetime.date.today().isoformat()

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO t_transaction (
                        transaction_date, category_id, amount, account_id,
                        entry_type, memo, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, now(), now())
                    """,
                    (
                        transaction_date,
                        category_id,
                        amount,
                        account_id,
                        entry_type,
                        memo,
                    ),
                )

        response_data = {
            "transaction_date": transaction_date,
            "category_id": category_id,
            "amount": amount,
            "account_id": account_id,
            "entry_type": entry_type,
            "memo": memo,
            "created_by": CREATED_BY,
            "updated_by": UPDATED_BY,
        }
        return func.HttpResponse(
            json.dumps({"message": "登録しました", "data": response_data}, ensure_ascii=False),
            mimetype="application/json",
            status_code=201,
        )
    except Exception as e:
        logging.error(f"RegisterTransaction error: {e}")
        return func.HttpResponse(
            json.dumps({"error": str(e)}, ensure_ascii=False),
            mimetype="application/json",
            status_code=500,
        )