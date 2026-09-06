import datetime as dt
import os
import secrets
from contextlib import contextmanager
from typing import Any

import altair as alt
import pandas as pd
import psycopg2
from psycopg2 import pool, sql
from psycopg2.extras import RealDictCursor
import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError


st.set_page_config(page_title="家計簿入力", page_icon="🧾", layout="wide")

st.markdown(
    """
    <style>
    .stApp {
        background: linear-gradient(135deg, #f7fbff 0%, #eef5ff 42%, #fff6ee 100%);
    }
    .block-container {
        padding-top: 2.4rem;
    }
    .app-title {
        font-size: 1.95rem;
        font-weight: 800;
        letter-spacing: 0.02em;
        color: #0b3558;
        line-height: 1.25;
        margin-top: 0.2rem;
    }
    .app-sub {
        color: #375a7f;
        margin-bottom: 0.8rem;
        line-height: 1.5;
    }
    .card {
        background: rgba(255,255,255,0.82);
        border: 1px solid #dae8f7;
        border-radius: 16px;
        padding: 0.8rem 1rem;
        box-shadow: 0 10px 28px rgba(11, 53, 88, 0.08);
    }
    /* 明細テーブル文字サイズを上げる */
    div[data-testid="stDataFrame"] div[role="columnheader"] {
        font-size: 1rem !important;
        font-weight: 700 !important;
    }
    div[data-testid="stDataFrame"] div[role="gridcell"] {
        font-size: 0.98rem !important;
    }
    /* 入力ラベル・説明文の視認性を確保 */
    [data-testid="stWidgetLabel"] p,
    [data-testid="stWidgetLabel"] span,
    [data-testid="stMarkdownContainer"] p,
    [data-testid="stCaptionContainer"] {
        color: #163b5c !important;
    }
    h3 {
        color: #173f67 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

def get_setting(name: str, default: str | None = None) -> str | None:
    try:
        if name in st.secrets:
            value = st.secrets.get(name)
            return str(value) if value is not None else default
    except StreamlitSecretNotFoundError:
        pass
    return os.getenv(name, default)


DB_SCHEMA = get_setting("DB_SCHEMA", "dwh")
DB_CONFIG = {
    "host": get_setting("DB_HOST", "localhost"),
    "port": int(get_setting("DB_PORT", "5432")),
    "dbname": get_setting("DB_NAME", "postgres"),
    "user": get_setting("DB_USER", "postgres"),
    "password": get_setting("DB_PASSWORD", ""),
    "sslmode": get_setting("DB_SSLMODE", "prefer"),
    "connect_timeout": int(get_setting("DB_CONNECT_TIMEOUT", "10")),
    "keepalives": int(get_setting("DB_KEEPALIVES", "1")),
    "keepalives_idle": int(get_setting("DB_KEEPALIVES_IDLE", "30")),
    "keepalives_interval": int(get_setting("DB_KEEPALIVES_INTERVAL", "10")),
    "keepalives_count": int(get_setting("DB_KEEPALIVES_COUNT", "3")),
}

RETRIABLE_DB_ERRORS = (psycopg2.OperationalError, psycopg2.InterfaceError)


def month_start(d: dt.date) -> dt.date:
    return dt.date(d.year, d.month, 1)


def month_end(d: dt.date) -> dt.date:
    if d.month == 12:
        next_month = dt.date(d.year + 1, 1, 1)
    else:
        next_month = dt.date(d.year, d.month + 1, 1)
    return next_month - dt.timedelta(days=1)


@st.cache_resource(show_spinner=False)
def get_pool():
    return pool.ThreadedConnectionPool(minconn=1, maxconn=8, **DB_CONFIG)


def reset_pool():
    try:
        get_pool().closeall()
    except Exception:
        pass
    get_pool.clear()


@contextmanager
def get_conn():
    p = None
    conn = None
    for attempt in range(2):
        p = get_pool()
        conn = p.getconn()
        try:
            with conn.cursor() as cur:
                # Borrowed connection may be stale; validate before use.
                cur.execute("SELECT 1")
                cur.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(DB_SCHEMA)))
            break
        except RETRIABLE_DB_ERRORS:
            p.putconn(conn, close=True)
            conn = None
            if attempt == 0:
                reset_pool()
                continue
            raise

    should_close = False
    try:
        yield conn
    except RETRIABLE_DB_ERRORS:
        should_close = True
        raise
    finally:
        if conn is not None and p is not None:
            p.putconn(conn, close=should_close or conn.closed != 0)


@st.cache_data(show_spinner=False)
def load_categories():
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT category_id, category_name FROM m_category ORDER BY category_name")
        return cur.fetchall()


@st.cache_data(show_spinner=False)
def load_accounts():
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT account_id, account_name FROM m_account ORDER BY account_name")
        return cur.fetchall()


def insert_transaction(
    transaction_date: dt.date,
    category_id: int,
    amount: float,
    account_id: int,
    entry_type: str,
    memo: str,
):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO t_transaction (
                transaction_date, category_id, amount, account_id,
                entry_type, memo, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, now(), now())
            """,
            (transaction_date, category_id, amount, account_id, entry_type, memo or None),
        )
        conn.commit()


def update_transaction(
    transaction_id: int,
    transaction_date: dt.date,
    category_id: int,
    amount: float,
    account_id: int,
    entry_type: str,
    memo: str,
):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE t_transaction
            SET
                transaction_date = %s,
                category_id = %s,
                amount = %s,
                account_id = %s,
                entry_type = %s,
                memo = %s,
                updated_at = now()
            WHERE transaction_id = %s
            """,
            (
                transaction_date,
                category_id,
                amount,
                account_id,
                entry_type,
                memo or None,
                transaction_id,
            ),
        )
        conn.commit()


def delete_transactions(transaction_ids: list[int]):
    if not transaction_ids:
        return
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM t_transaction WHERE transaction_id = ANY(%s)", (transaction_ids,))
        conn.commit()


def _sort_order_clause(sort_key: str, descending: bool) -> str:
    sort_map = {
        "日付": "t.transaction_date",
        "金額": "t.amount",
    }
    col = sort_map.get(sort_key, "t.transaction_date")
    direction = "DESC" if descending else "ASC"
    return f"{col} {direction}, t.transaction_id ASC"


@st.cache_data(show_spinner=False)
def load_month_bounds() -> tuple[dt.date, dt.date]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT MIN(transaction_date), MAX(transaction_date) FROM t_transaction")
        min_date, max_date = cur.fetchone()
    today = dt.date.today()
    if min_date is None or max_date is None:
        return month_start(today), month_start(today)
    return month_start(min_date), month_start(max_date)


def build_month_options(start_month: dt.date, end_month: dt.date) -> list[dt.date]:
    options = []
    cur = dt.date(start_month.year, start_month.month, 1)
    last = dt.date(end_month.year, end_month.month, 1)
    while cur <= last:
        options.append(cur)
        if cur.month == 12:
            cur = dt.date(cur.year + 1, 1, 1)
        else:
            cur = dt.date(cur.year, cur.month + 1, 1)
    return options


def load_transactions(start_date: dt.date, end_date: dt.date, sort_key: str, descending: bool) -> pd.DataFrame:
    expected_columns = [
        "transaction_id",
        "transaction_date",
        "category_name",
        "amount",
        "account_name",
        "entry_type",
        "memo",
    ]
    order_clause = _sort_order_clause(sort_key, descending)
    query = f"""
        SELECT
            t.transaction_id,
            t.transaction_date,
            c.category_name,
            t.amount,
            a.account_name,
            t.entry_type,
            COALESCE(t.memo, '') AS memo
        FROM t_transaction t
        JOIN m_category c ON c.category_id = t.category_id
        JOIN m_account a ON a.account_id = t.account_id
        WHERE t.transaction_date BETWEEN %s AND %s
        ORDER BY {order_clause}
    """
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, (start_date, end_date))
        rows = cur.fetchall()
    if not rows:
        return pd.DataFrame(columns=expected_columns)
    return pd.DataFrame(rows, columns=expected_columns)


def load_budget_progress(as_of_date: dt.date) -> dict[str, Any] | None:
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
    with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, (fiscal_year, as_of_date, as_of_date))
        row = cur.fetchone()
    return row


def normalize_entry_type_display(value: str) -> str:
    return {"expense": "支出", "income": "収入"}.get(value, value)


def entry_type_for_db(value: str) -> str:
    return {"支出": "expense", "収入": "income", "expense": "expense", "income": "income"}.get(value, "expense")


def ensure_db_settings():
    required_keys = ["DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"]
    missing = [k for k in required_keys if not get_setting(k)]
    if missing:
        st.error("DB接続設定が不足しています。Streamlit Secrets か環境変数を設定してください。")
        st.caption("不足キー: " + ", ".join(missing))
        st.stop()


AUTH_ENABLED = (get_setting("AUTH_ENABLED", "false") or "false").lower() == "true"
AUTH_STAGE = (get_setting("AUTH_STAGE", "password") or "password").lower()
OAUTH_PROVIDER = (get_setting("OAUTH_PROVIDER", "") or "").strip()
APP_PASSWORD = get_setting("APP_PASSWORD", "") or ""
APP_ALLOWED_EMAILS = get_setting("APP_ALLOWED_EMAILS", "") or ""
AUTH_MAX_ATTEMPTS = int(get_setting("AUTH_MAX_ATTEMPTS", "5") or "5")
AUTH_LOCK_MINUTES = int(get_setting("AUTH_LOCK_MINUTES", "15") or "15")
AUTH_SESSION_TTL_MINUTES = int(get_setting("AUTH_SESSION_TTL_MINUTES", "480") or "480")
AUTH_SHOW_AUDIT = (get_setting("AUTH_SHOW_AUDIT", "true") or "true").lower() == "true"


def parse_allowed_emails(raw: str) -> set[str]:
    return {
        item.strip().lower()
        for item in raw.replace(";", ",").split(",")
        if item.strip()
    }


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_iso_datetime(raw: str) -> dt.datetime | None:
    try:
        value = dt.datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def append_auth_audit(event: str, detail: str = ""):
    logs = st.session_state.get("auth_audit_logs", [])
    logs.append(
        {
            "time": utcnow().isoformat(timespec="seconds"),
            "event": event,
            "detail": detail,
        }
    )
    st.session_state["auth_audit_logs"] = logs[-30:]


def clear_auth_session():
    st.session_state.pop("auth_ok", None)
    st.session_state.pop("auth_time", None)


def get_oauth_user_email() -> str:
    user_obj = getattr(st, "user", None)
    if user_obj is None:
        return ""

    # Provider-specific claims differ; Entra often returns preferred_username.
    attr_candidates = [
        "email",
        "preferred_username",
        "upn",
        "user_principal_name",
    ]
    for attr_name in attr_candidates:
        value = getattr(user_obj, attr_name, "")
        normalized = str(value or "").strip().lower()
        if normalized:
            return normalized

    get_fn = getattr(user_obj, "get", None)
    if callable(get_fn):
        for key in attr_candidates:
            value = get_fn(key, "")
            normalized = str(value or "").strip().lower()
            if normalized:
                return normalized

    return ""


def get_oauth_identity_candidates(identity: str) -> set[str]:
    normalized = str(identity or "").strip().lower()
    if not normalized:
        return set()

    candidates = {normalized}

    # Entra guest UPN may look like user_domain.com#EXT#@tenant.onmicrosoft.com.
    # Add a reconstructed primary email candidate for allowlist matching.
    marker = "#ext#@"
    marker_pos = normalized.find(marker)
    if marker_pos > 0:
        guest_prefix = normalized[:marker_pos]
        if "_" in guest_prefix:
            reconstructed = guest_prefix.replace("_", "@", 1)
            candidates.add(reconstructed)

    return candidates


def is_oauth_logged_in() -> bool:
    user_obj = getattr(st, "user", None)
    if user_obj is None:
        return False
    return bool(getattr(user_obj, "is_logged_in", False))


def oauth_settings_error() -> str | None:
    try:
        secrets_root = st.secrets
    except StreamlitSecretNotFoundError:
        return "Secrets が未設定です。Streamlit 側に OAuth 設定を追加してください。"

    auth_cfg = secrets_root.get("auth")
    if auth_cfg is None:
        return "Secrets の [auth] セクションが見つかりません。"

    required_shared = ["redirect_uri", "cookie_secret"]
    for key in required_shared:
        if not auth_cfg.get(key):
            return f"[auth] の {key} が未設定です。"

    required_provider = ["client_id", "client_secret", "server_metadata_url"]

    provider_name = OAUTH_PROVIDER
    if not provider_name and auth_cfg.get("microsoft"):
        provider_name = "microsoft"

    if provider_name:
        provider_cfg = auth_cfg.get(provider_name)
        if provider_cfg is None:
            return f"[auth.{provider_name}] が未設定です。"
        for key in required_provider:
            if not provider_cfg.get(key):
                return f"[auth.{provider_name}] の {key} が未設定です。"
        return None

    for key in required_provider:
        if not auth_cfg.get(key):
            return f"[auth] の {key} が未設定です。"
    return None


def ensure_oauth_authenticated():
    if not hasattr(st, "login") or not hasattr(st, "logout"):
        st.error("この環境の Streamlit では OAuth 機能が使えません。バージョンを更新してください。")
        st.stop()

    settings_error = oauth_settings_error()
    if settings_error:
        st.error("OAuth 設定に不備があります。")
        st.caption(settings_error)
        st.stop()

    provider_name = OAUTH_PROVIDER
    try:
        auth_cfg = st.secrets.get("auth")
        if not provider_name and auth_cfg and auth_cfg.get("microsoft"):
            provider_name = "microsoft"
    except StreamlitSecretNotFoundError:
        provider_name = OAUTH_PROVIDER

    if not is_oauth_logged_in():
        st.markdown("## Private Access")
        st.caption("このアプリは OAuth ログインが必要です。")
        if st.button("OAuthでログイン", use_container_width=True):
            append_auth_audit("oauth_login_start", "user_action")
            try:
                if provider_name:
                    st.login(provider_name)
                else:
                    st.login()
            except Exception as ex:
                append_auth_audit("oauth_login_error", str(ex))
                st.error("OAuth ログイン開始時にエラーが発生しました。")
                st.caption(str(ex))
                st.info(
                    "確認ポイント: [auth] の redirect_uri / client_id / client_secret / "
                    "server_metadata_url と Entra 側 Redirect URI の完全一致"
                )
        st.stop()

    oauth_email = get_oauth_user_email()
    oauth_candidates = get_oauth_identity_candidates(oauth_email)
    allowlist = parse_allowed_emails(APP_ALLOWED_EMAILS)
    if not oauth_email:
        append_auth_audit("oauth_missing_email_claim", "no_email_or_upn")
        st.error(
            "IDプロバイダーからメール情報を取得できませんでした。"
            " Entra 側で email か preferred_username のクレームを確認してください。"
        )
        if st.button("OAuthログアウト", use_container_width=True):
            st.logout()
        st.stop()

    if allowlist and allowlist.isdisjoint(oauth_candidates):
        append_auth_audit("oauth_email_rejected", oauth_email or "empty_email")
        st.error("このメールアドレスは許可されていません。")
        st.caption("検出された識別子: " + ", ".join(sorted(oauth_candidates)))
        st.caption("APP_ALLOWED_EMAILS に上記いずれかを追加してください。")
        if st.button("OAuthログアウト", use_container_width=True):
            try:
                st.logout()
            except Exception as ex:
                st.caption(str(ex))
        st.stop()

    st.session_state["auth_ok"] = True
    st.session_state["auth_time"] = utcnow().isoformat()
    st.session_state["auth_oauth_email"] = oauth_email
    append_auth_audit("oauth_login_success", oauth_email or "unknown")


def require_authentication():
    if not AUTH_ENABLED:
        return

    if st.session_state.get("auth_ok"):
        auth_time = parse_iso_datetime(st.session_state.get("auth_time", ""))
        if auth_time is None:
            clear_auth_session()
            append_auth_audit("session_reset", "invalid_auth_time")
        else:
            ttl_deadline = auth_time + dt.timedelta(minutes=max(AUTH_SESSION_TTL_MINUTES, 1))
            if utcnow() <= ttl_deadline:
                return
            clear_auth_session()
            append_auth_audit("session_expired", f"ttl={AUTH_SESSION_TTL_MINUTES}m")
            st.warning("セッションの有効期限が切れたため、再ログインしてください。")

    if AUTH_STAGE == "oauth":
        ensure_oauth_authenticated()
        return

    st.markdown("## Private Access")
    st.caption("このアプリは認証が必要です。")

    if not APP_PASSWORD:
        st.error("APP_PASSWORD が未設定です。Streamlit Secrets を設定してください。")
        st.stop()

    max_attempts = max(AUTH_MAX_ATTEMPTS, 1)
    lock_minutes = max(AUTH_LOCK_MINUTES, 1)
    failed_attempts = int(st.session_state.get("auth_failed_attempts", 0))
    lock_until = parse_iso_datetime(st.session_state.get("auth_lock_until", ""))

    if lock_until and utcnow() < lock_until:
        remain = lock_until - utcnow()
        remain_minutes = max(1, int(remain.total_seconds() // 60) + 1)
        st.error(f"ログイン試行回数が上限に達しました。約 {remain_minutes} 分後に再試行してください。")
        st.stop()

    if lock_until and utcnow() >= lock_until:
        st.session_state["auth_lock_until"] = ""
        st.session_state["auth_failed_attempts"] = 0
        append_auth_audit("lock_released", "cooldown_finished")

    with st.form("auth_form", clear_on_submit=False):
        email = ""
        if AUTH_STAGE == "allowlist":
            email = (st.text_input("メールアドレス", placeholder="you@example.com") or "").strip().lower()
        password = st.text_input("共有パスワード", type="password")
        submitted = st.form_submit_button("ログイン", use_container_width=True)

    if submitted:
        def mark_failed(reason: str):
            attempts = int(st.session_state.get("auth_failed_attempts", 0)) + 1
            st.session_state["auth_failed_attempts"] = attempts
            append_auth_audit("login_failed", reason)
            if attempts >= max_attempts:
                locked_until = utcnow() + dt.timedelta(minutes=lock_minutes)
                st.session_state["auth_lock_until"] = locked_until.isoformat()
                append_auth_audit("locked", f"for={lock_minutes}m")
                st.error(f"試行回数が上限に達しました。{lock_minutes} 分間ロックします。")
            else:
                st.error(f"認証に失敗しました。残り {max_attempts - attempts} 回です。")

        if AUTH_STAGE == "allowlist":
            allowlist = parse_allowed_emails(APP_ALLOWED_EMAILS)
            if not email:
                mark_failed("empty_email")
                st.stop()
            if not allowlist:
                st.error("APP_ALLOWED_EMAILS が未設定です。")
                st.stop()
            if email not in allowlist:
                mark_failed("email_not_allowed")
                st.stop()

        if secrets.compare_digest(password, APP_PASSWORD):
            st.session_state["auth_ok"] = True
            st.session_state["auth_time"] = utcnow().isoformat()
            st.session_state["auth_failed_attempts"] = 0
            st.session_state["auth_lock_until"] = ""
            append_auth_audit("login_success", AUTH_STAGE)
            st.rerun()
        else:
            mark_failed("wrong_password")

    st.stop()


st.markdown('<div class="app-title">家計簿アプリ</div>', unsafe_allow_html=True)
st.markdown('<div class="app-sub">記帳・明細確認・修正・削除をひとつの画面で操作できます</div>', unsafe_allow_html=True)

require_authentication()

if AUTH_ENABLED and st.session_state.get("auth_ok"):
    with st.sidebar:
        st.caption("認証済み")
        if AUTH_STAGE == "oauth":
            oauth_email = st.session_state.get("auth_oauth_email", "")
            if oauth_email:
                st.caption(f"OAuth: {oauth_email}")
        auth_time_raw = st.session_state.get("auth_time", "")
        auth_time = parse_iso_datetime(auth_time_raw)
        if auth_time:
            expires_at = auth_time + dt.timedelta(minutes=max(AUTH_SESSION_TTL_MINUTES, 1))
            st.caption(f"有効期限(UTC): {expires_at.strftime('%Y-%m-%d %H:%M')}")

        if AUTH_SHOW_AUDIT:
            with st.expander("認証ログ(このセッション)", expanded=False):
                logs = st.session_state.get("auth_audit_logs", [])
                if not logs:
                    st.caption("まだログはありません。")
                else:
                    log_df = pd.DataFrame(logs)
                    st.dataframe(log_df.iloc[::-1], width="stretch", hide_index=True)

        if st.button("ログアウト", use_container_width=True):
            append_auth_audit("logout", "user_action")
            clear_auth_session()
            if AUTH_STAGE == "oauth" and hasattr(st, "logout"):
                try:
                    st.logout()
                except Exception as ex:
                    st.caption(str(ex))
            st.rerun()

ensure_db_settings()

categories = load_categories()
accounts = load_accounts()

if not categories or not accounts:
    st.error("カテゴリまたはアカウントのマスタが空です。先にDBのマスタを作成してください。")
    st.stop()

all_category_names = [row["category_name"] for row in categories]
default_categories = [name for name in all_category_names if name != "固定費"]
if not default_categories:
    default_categories = all_category_names

CATEGORY_FILTER_KEY = "category_filter"
if CATEGORY_FILTER_KEY not in st.session_state:
    st.session_state[CATEGORY_FILTER_KEY] = default_categories

ANALYSIS_CATEGORY_FILTER_KEY = "analysis_category_filter"
if ANALYSIS_CATEGORY_FILTER_KEY not in st.session_state:
    st.session_state[ANALYSIS_CATEGORY_FILTER_KEY] = default_categories

SORT_KEY_NAME = "sort_key_name"
SORT_DIRECTION_NAME = "sort_direction_name"
if SORT_KEY_NAME not in st.session_state:
    st.session_state[SORT_KEY_NAME] = "日付"
if SORT_DIRECTION_NAME not in st.session_state:
    st.session_state[SORT_DIRECTION_NAME] = "降順"

DETAIL_MONTH_KEY = "detail_month"
ANALYSIS_YEAR_KEY = "analysis_year"
ANALYSIS_MODE_KEY = "analysis_mode"
ANALYSIS_COMPARE_YEARS_KEY = "analysis_compare_years"


def build_month_selector_options() -> tuple[dt.date, list[dt.date], int]:
    today_month = month_start(dt.date.today())
    current_year_end_month = dt.date(today_month.year, 12, 1)
    min_month, max_month = load_month_bounds()
    start_month = min(min_month, today_month)
    end_month = max(max_month, current_year_end_month)
    month_options = build_month_options(start_month, end_month)
    month_options_desc = list(reversed(month_options))
    default_index = month_options_desc.index(today_month) if today_month in month_options_desc else 0
    return today_month, month_options_desc, default_index


def resolve_selected_categories(raw_values: list[str], category_names: list[str]) -> list[str]:
    normalized = [c for c in raw_values if c == "全選択" or c in category_names]
    if "全選択" in normalized:
        return category_names
    return normalized


def build_pretty_color_scale(series: pd.Series) -> alt.Scale:
    domain = sorted({str(v) for v in series.dropna().tolist()})
    palette = [
        "#0ea5e9", "#14b8a6", "#22c55e", "#f59e0b", "#f97316", "#ef4444",
        "#ec4899", "#8b5cf6", "#6366f1", "#06b6d4", "#84cc16", "#a855f7",
    ]
    if not domain:
        return alt.Scale(range=palette)
    expanded = [palette[i % len(palette)] for i in range(len(domain))]
    return alt.Scale(domain=domain, range=expanded)


def is_iphone_access() -> bool:
    try:
        ctx = getattr(st, "context", None)
        headers = getattr(ctx, "headers", None)
        if headers is None:
            return False
        user_agent = str(headers.get("user-agent", "") or "").lower()
        return "iphone" in user_agent
    except Exception:
        return False


iphone_access = is_iphone_access()

if iphone_access:
    tab_detail, tab_entry = st.tabs(["明細", "記帳"])
    tab_analysis = None
else:
    tab_detail, tab_analysis, tab_entry = st.tabs(["明細", "分析", "記帳"])

with tab_entry:
    with st.container(border=True):
        st.subheader("記帳")
        st.caption("入力項目を指定して取引を登録します")
        with st.form("expense_form", clear_on_submit=True):
            input_date = st.date_input("日付", value=dt.date.today(), format="YYYY/MM/DD")

            category_options = {row["category_name"]: row["category_id"] for row in categories}
            category_name = st.selectbox("カテゴリ", list(category_options.keys()), index=0)

            account_options = {row["account_name"]: row["account_id"] for row in accounts}
            default_account_idx = 0
            account_names = list(account_options.keys())
            if "クレジット" in account_names:
                default_account_idx = account_names.index("クレジット")
            account_name = st.selectbox("アカウント", account_names, index=default_account_idx)

            entry_type = st.segmented_control(
                "収支区分",
                options=["expense", "income"],
                default="expense",
                format_func=lambda x: "支出" if x == "expense" else "収入",
            )
            amount = st.number_input("金額", min_value=0.0, step=1.0, format="%.0f")
            memo = st.text_input("メモ")

            submitted = st.form_submit_button("登録", use_container_width=True)
            if submitted:
                if amount <= 0:
                    st.warning("金額は 1 以上で入力してください。")
                else:
                    insert_transaction(
                        transaction_date=input_date,
                        category_id=category_options[category_name],
                        amount=amount,
                        account_id=account_options[account_name],
                        entry_type=entry_type,
                        memo=memo,
                    )
                    st.success("登録しました。")
                    st.rerun()

with tab_detail:
    with st.container(border=True):
        st.subheader("明細")
        _, month_options_desc, default_index = build_month_selector_options()

        if DETAIL_MONTH_KEY not in st.session_state:
            st.session_state[DETAIL_MONTH_KEY] = month_options_desc[default_index]

        selected_month = st.selectbox(
            "対象月",
            options=month_options_desc,
            key=DETAIL_MONTH_KEY,
            format_func=lambda d: d.strftime("%Y年%m月"),
            help="表示する月を選択してください。",
        )

        sort_key = st.session_state.get(SORT_KEY_NAME, "日付")
        sort_direction = st.session_state.get(SORT_DIRECTION_NAME, "降順")

        selected_categories_raw = st.session_state.get(CATEGORY_FILTER_KEY, default_categories)
        selected_categories = resolve_selected_categories(selected_categories_raw, all_category_names)

        start_date = month_start(selected_month)
        end_date = month_end(selected_month)
        descending = sort_direction == "降順"
        st.caption(f"表示期間: {start_date} 〜 {end_date}")

        df = load_transactions(start_date=start_date, end_date=end_date, sort_key=sort_key, descending=descending)

        if selected_categories:
            df = df[df["category_name"].isin(selected_categories)].copy()
        else:
            df = df.iloc[0:0].copy()

        year_start = dt.date(selected_month.year, 1, 1)
        ytd_df = load_transactions(
            start_date=year_start,
            end_date=end_date,
            sort_key="日付",
            descending=False,
        )
        if selected_categories:
            ytd_df = ytd_df[ytd_df["category_name"].isin(selected_categories)].copy()
        else:
            ytd_df = ytd_df.iloc[0:0].copy()

        monthly_expense_total = (
            float(df[df["entry_type"] == "expense"]["amount"].sum()) if not df.empty else 0.0
        )
        ytd_expense_total = (
            float(ytd_df[ytd_df["entry_type"] == "expense"]["amount"].sum()) if not ytd_df.empty else 0.0
        )

        budget_progress = load_budget_progress(end_date)

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("支出の合計", f"{monthly_expense_total:,.0f}")
        c2.metric("これまでの合計額（年額）", f"{ytd_expense_total:,.0f}")
        if budget_progress:
            progress_rate = float(budget_progress["progress_rate_percent"])
            annual_budget = float(budget_progress["annual_budget"])
            denominator = float(budget_progress["denominator"])
            monthly_balance = denominator - ytd_expense_total
            annual_balance = annual_budget - ytd_expense_total

            c3.metric("予算進捗率", f"{progress_rate:.2f}%")
            c4.metric("残高（月間）", f"{monthly_balance:,.0f}")
            c5.metric("残高（年間）", f"{annual_balance:,.0f}")
            st.caption(
                "予算進捗: "
                f"{float(budget_progress['numerator']):,.0f} / {float(budget_progress['denominator']):,.0f} "
                "(固定費除外)"
            )
        else:
            c3.metric("予算進捗率", "- %")
            c4.metric("残高（月間）", "-")
            c5.metric("残高（年間）", "-")
            st.caption("該当年の予算マスタが未登録です。")

        if df.empty:
            st.info("対象期間の明細はありません。")
        else:
            display_df = pd.DataFrame(
                {
                    "削除": False,
                    "日付": pd.to_datetime(df["transaction_date"]).dt.date,
                    "カテゴリ": df["category_name"],
                    "金額": df["amount"].astype(float),
                    "アカウント": df["account_name"],
                    "収支区分": df["entry_type"].map(normalize_entry_type_display),
                    "メモ": df["memo"],
                }
            )
            display_df.index = df["transaction_id"].astype(int)

            category_names = all_category_names
            account_names = [row["account_name"] for row in accounts]
            edited_df = st.data_editor(
                display_df,
                width="content",
                hide_index=True,
                key="transactions_editor",
                disabled=[],
                column_config={
                    "削除": st.column_config.CheckboxColumn("削除", help="削除する行をオン", width="small"),
                    "日付": st.column_config.DateColumn("日付", format="YYYY/MM/DD", width="medium"),
                    "カテゴリ": st.column_config.SelectboxColumn("カテゴリ", options=category_names, required=True, width="medium"),
                    "金額": st.column_config.NumberColumn("金額", min_value=0.0, step=1.0, format="%.0f", width="small"),
                    "アカウント": st.column_config.SelectboxColumn("アカウント", options=account_names, required=True, width="medium"),
                    "収支区分": st.column_config.SelectboxColumn("収支区分", options=["支出", "収入"], required=True, width="small"),
                    "メモ": st.column_config.TextColumn("メモ", width="large"),
                },
            )

            save_clicked = st.button("変更を保存", type="primary", use_container_width=True)

            if save_clicked:
                category_to_id = {row["category_name"]: row["category_id"] for row in categories}
                account_to_id = {row["account_name"]: row["account_id"] for row in accounts}

                original_by_id: dict[int, dict[str, Any]] = {
                    int(r["transaction_id"]): r for r in df.to_dict(orient="records")
                }

                delete_ids: list[int] = []
                update_rows: list[dict[str, Any]] = []

                for tx_id, row in edited_df.iterrows():
                    tx_id = int(tx_id)
                    if bool(row.get("削除")):
                        delete_ids.append(tx_id)
                        continue

                    if row.get("カテゴリ") not in category_to_id or row.get("アカウント") not in account_to_id:
                        st.error(f"ID {tx_id}: カテゴリまたはアカウントの選択値が不正です。")
                        st.stop()

                    amount_value = float(row.get("金額", 0))
                    if amount_value <= 0:
                        st.error(f"ID {tx_id}: 金額は 1 以上で入力してください。")
                        st.stop()

                    old = original_by_id[tx_id]
                    new_date = row.get("日付")
                    if isinstance(new_date, pd.Timestamp):
                        new_date = new_date.date()

                    has_changed = (
                        new_date != old["transaction_date"]
                        or str(row.get("カテゴリ")) != str(old["category_name"])
                        or float(amount_value) != float(old["amount"])
                        or str(row.get("アカウント")) != str(old["account_name"])
                        or entry_type_for_db(str(row.get("収支区分"))) != str(old["entry_type"])
                        or str(row.get("メモ", "")) != str(old.get("memo", ""))
                    )

                    if has_changed:
                        update_rows.append(
                            {
                                "transaction_id": tx_id,
                                "transaction_date": new_date,
                                "category_id": category_to_id[str(row.get("カテゴリ"))],
                                "amount": amount_value,
                                "account_id": account_to_id[str(row.get("アカウント"))],
                                "entry_type": entry_type_for_db(str(row.get("収支区分"))),
                                "memo": str(row.get("メモ", "")),
                            }
                        )

                for u in update_rows:
                    update_transaction(
                        transaction_id=u["transaction_id"],
                        transaction_date=u["transaction_date"],
                        category_id=u["category_id"],
                        amount=u["amount"],
                        account_id=u["account_id"],
                        entry_type=u["entry_type"],
                        memo=u["memo"],
                    )

                if delete_ids:
                    delete_transactions(delete_ids)

                if update_rows or delete_ids:
                    st.success(f"更新: {len(update_rows)}件 / 削除: {len(delete_ids)}件 を反映しました。")
                    st.rerun()
                else:
                    st.info("変更はありません。")

        sort_col1, sort_col2, _ = st.columns([0.32, 0.32, 0.36])
        with sort_col1:
            st.selectbox(
                "並び替え項目",
                ["日付", "金額"],
                key=SORT_KEY_NAME,
            )
        with sort_col2:
            st.selectbox(
                "並び順",
                ["降順", "昇順"],
                key=SORT_DIRECTION_NAME,
            )

        st.multiselect(
            "カテゴリフィルター",
            options=["全選択"] + all_category_names,
            key=CATEGORY_FILTER_KEY,
            help="全選択を選ぶと、すべてのカテゴリを対象にします。固定費は初期状態で除外しています。",
        )

if tab_analysis is not None:
    with tab_analysis:
        with st.container(border=True):
            st.subheader("分析")
            st.caption("月間は対象年の月比較、年間は年同士の比較を表示します（固定費は初期状態で除外）。")

            analysis_mode = st.segmented_control(
                "表示モード",
                options=["月間", "年間"],
                default=st.session_state.get(ANALYSIS_MODE_KEY, "月間"),
                key=ANALYSIS_MODE_KEY,
                help="ボタンで月間比較 / 年間比較を切り替えます。",
            )

            current_year = dt.date.today().year
            _, max_month = load_month_bounds()
            max_year = max(max_month.year, current_year)
            year_options = list(range(max_year, 2020, -1))
            if not year_options:
                year_options = [current_year]

            if ANALYSIS_YEAR_KEY not in st.session_state:
                st.session_state[ANALYSIS_YEAR_KEY] = current_year if current_year in year_options else year_options[0]

            analysis_year = st.selectbox(
                "対象年",
                options=year_options,
                key=ANALYSIS_YEAR_KEY,
                help="月間比較で使用する年を選択してください。",
            )

            analysis_categories_raw = st.session_state.get(ANALYSIS_CATEGORY_FILTER_KEY, default_categories)
            analysis_categories = resolve_selected_categories(analysis_categories_raw, all_category_names)

            if analysis_mode == "月間":
                month_start_date = dt.date(analysis_year, 1, 1)
                month_end_date = dt.date(analysis_year, 12, 31)
                month_base_df = load_transactions(
                    start_date=month_start_date,
                    end_date=month_end_date,
                    sort_key="日付",
                    descending=False,
                )

                if analysis_categories:
                    month_base_df = month_base_df[month_base_df["category_name"].isin(analysis_categories)].copy()
                else:
                    month_base_df = month_base_df.iloc[0:0].copy()

                month_expense_df = month_base_df[month_base_df["entry_type"] == "expense"].copy()
                month_total = float(month_expense_df["amount"].sum()) if not month_expense_df.empty else 0.0
                if analysis_year == current_year:
                    months_for_avg = max(1, dt.date.today().month)
                else:
                    months_for_avg = 12
                month_summary_col1, month_summary_col2 = st.columns(2)
                month_summary_col1.metric(f"{analysis_year}年 支出合計", f"{month_total:,.0f}円")
                month_summary_col2.metric("月平均", f"{(month_total / months_for_avg):,.0f}円")

                monthly_chart_df = pd.DataFrame(columns=["month_num", "月", "カテゴリ", "金額"])
                if not month_expense_df.empty:
                    monthly_chart_df = month_expense_df.copy()
                    monthly_chart_df["month_num"] = pd.to_datetime(monthly_chart_df["transaction_date"]).dt.month
                    monthly_chart_df["月"] = monthly_chart_df["month_num"].apply(lambda m: f"{int(m)}月")
                    monthly_chart_df = (
                        monthly_chart_df.groupby(["month_num", "月", "category_name"], as_index=False)["amount"]
                        .sum()
                        .rename(columns={"category_name": "カテゴリ", "amount": "金額"})
                        .sort_values(["month_num", "カテゴリ"])
                    )
                    monthly_chart_df["金額"] = monthly_chart_df["金額"].astype(float)

                st.markdown(f"#### 月間比較（{analysis_year}年 1月-12月）")
                if monthly_chart_df.empty:
                    st.info("対象年の支出データがありません。")
                else:
                    month_order = [f"{i}月" for i in range(1, 13)]
                    monthly_bar_chart = (
                        alt.Chart(monthly_chart_df)
                        .mark_bar()
                        .encode(
                            x=alt.X("月:N", sort=month_order, title="月"),
                            y=alt.Y(
                                "金額:Q",
                                title="金額（円）",
                                axis=alt.Axis(format=",.0f", labelExpr="format(datum.value, ',.0f')"),
                            ),
                            color=alt.Color(
                                "カテゴリ:N",
                                scale=build_pretty_color_scale(monthly_chart_df["カテゴリ"]),
                                legend=alt.Legend(title="カテゴリ"),
                            ),
                            tooltip=["月:N", "カテゴリ:N", alt.Tooltip("金額:Q", format=",.0f")],
                        )
                    )
                    monthly_totals_df = (
                        monthly_chart_df.groupby(["month_num", "月"], as_index=False)["金額"]
                        .sum()
                        .sort_values("month_num")
                    )
                    monthly_total_labels = (
                        alt.Chart(monthly_totals_df)
                        .mark_text(dy=-8, color="#6b7280", fontSize=12, fontWeight="bold")
                        .encode(
                            x=alt.X("月:N", sort=month_order),
                            y=alt.Y("金額:Q"),
                            text=alt.Text("金額:Q", format=",.0f"),
                        )
                    )
                    monthly_chart = alt.layer(monthly_bar_chart, monthly_total_labels).properties(height=380)
                    st.altair_chart(monthly_chart, use_container_width=True)
            else:
                if ANALYSIS_COMPARE_YEARS_KEY not in st.session_state:
                    default_compare_years = [
                        y for y in [analysis_year, analysis_year - 1, analysis_year - 2] if y in year_options
                    ]
                    st.session_state[ANALYSIS_COMPARE_YEARS_KEY] = default_compare_years or [analysis_year]
                else:
                    current_compare_years = [
                        y for y in st.session_state.get(ANALYSIS_COMPARE_YEARS_KEY, []) if y in year_options
                    ]
                    if not current_compare_years:
                        current_compare_years = [analysis_year]
                    st.session_state[ANALYSIS_COMPARE_YEARS_KEY] = current_compare_years

                compare_years = st.multiselect(
                    "比較する年",
                    options=year_options,
                    key=ANALYSIS_COMPARE_YEARS_KEY,
                    help="年間比較で表示する年を選択してください。",
                )

                if not compare_years:
                    st.info("年間比較を表示するには、比較する年を1つ以上選択してください。")
                else:
                    compare_years = sorted(compare_years)
                    annual_start = dt.date(min(compare_years), 1, 1)
                    annual_end = dt.date(max(compare_years), 12, 31)
                    annual_base_df = load_transactions(
                        start_date=annual_start,
                        end_date=annual_end,
                        sort_key="日付",
                        descending=False,
                    )
                    annual_base_df["年"] = pd.to_datetime(annual_base_df["transaction_date"]).dt.year
                    annual_base_df = annual_base_df[annual_base_df["年"].isin(compare_years)].copy()

                    if analysis_categories:
                        annual_base_df = annual_base_df[annual_base_df["category_name"].isin(analysis_categories)].copy()
                    else:
                        annual_base_df = annual_base_df.iloc[0:0].copy()

                    annual_expense_df = annual_base_df[annual_base_df["entry_type"] == "expense"].copy()
                    annual_total = float(annual_expense_df["amount"].sum()) if not annual_expense_df.empty else 0.0
                    st.metric("比較対象年の支出合計", f"{annual_total:,.0f}円")

                    annual_chart_df = pd.DataFrame(columns=["年", "カテゴリ", "金額"])
                    if not annual_expense_df.empty:
                        annual_chart_df = (
                            annual_expense_df.groupby(["年", "category_name"], as_index=False)["amount"]
                            .sum()
                            .rename(columns={"category_name": "カテゴリ", "amount": "金額"})
                        )
                        annual_chart_df["金額"] = annual_chart_df["金額"].astype(float)

                    st.markdown("#### 年間比較（年同士）")
                    if annual_chart_df.empty:
                        st.info("選択した年の支出データがありません。")
                    else:
                        annual_bar_chart = (
                            alt.Chart(annual_chart_df)
                            .mark_bar()
                            .encode(
                                x=alt.X("年:O", sort=compare_years, title="年"),
                                y=alt.Y(
                                    "金額:Q",
                                    title="金額（円）",
                                    axis=alt.Axis(format=",.0f", labelExpr="format(datum.value, ',.0f')"),
                                ),
                                color=alt.Color(
                                    "カテゴリ:N",
                                    scale=build_pretty_color_scale(annual_chart_df["カテゴリ"]),
                                    legend=alt.Legend(title="カテゴリ"),
                                ),
                                tooltip=["年:O", "カテゴリ:N", alt.Tooltip("金額:Q", format=",.0f")],
                            )
                        )
                        annual_totals_df = (
                            annual_chart_df.groupby("年", as_index=False)["金額"]
                            .sum()
                            .sort_values("年")
                        )
                        annual_total_labels = (
                            alt.Chart(annual_totals_df)
                            .mark_text(dy=-8, color="#6b7280", fontSize=12, fontWeight="bold")
                            .encode(
                                x=alt.X("年:O", sort=compare_years),
                                y=alt.Y("金額:Q"),
                                text=alt.Text("金額:Q", format=",.0f"),
                            )
                        )
                        annual_chart = alt.layer(annual_bar_chart, annual_total_labels).properties(height=380)
                        st.altair_chart(annual_chart, use_container_width=True)

            st.multiselect(
                "カテゴリフィルター",
                options=["全選択"] + all_category_names,
                key=ANALYSIS_CATEGORY_FILTER_KEY,
                help="全選択を選ぶと、すべてのカテゴリを対象にします。固定費は初期状態で除外しています。",
            )
