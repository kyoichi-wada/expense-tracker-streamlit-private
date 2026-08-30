CREATE SCHEMA IF NOT EXISTS dwh;

-- =========================================
-- 収支区分の型
-- =========================================
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_type t
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE t.typname = 'entry_type'
          AND n.nspname = 'dwh'
    ) THEN
        CREATE TYPE dwh.entry_type AS ENUM ('income', 'expense');
    END IF;
END $$;


-- =========================================
-- マスタ: カテゴリ
-- =========================================
CREATE TABLE IF NOT EXISTS dwh.m_category (
    category_id    INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    category_name  VARCHAR(50) NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE dwh.m_category IS 'カテゴリマスタ';


-- =========================================
-- マスタ: アカウント
-- =========================================
CREATE TABLE IF NOT EXISTS dwh.m_account (
    account_id    INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_name  VARCHAR(50) NOT NULL UNIQUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE dwh.m_account IS 'アカウント（支払元/入金先）マスタ';


-- =========================================
-- トラン: 取引
-- =========================================
CREATE TABLE IF NOT EXISTS dwh.t_transaction (
    transaction_id    INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_date  DATE       NOT NULL,
    category_id       INTEGER    NOT NULL REFERENCES dwh.m_category(category_id) ON DELETE RESTRICT,
    amount            NUMERIC(12, 2) NOT NULL CHECK (amount >= 0),
    account_id        INTEGER    NOT NULL REFERENCES dwh.m_account(account_id) ON DELETE RESTRICT,
    entry_type        dwh.entry_type NOT NULL,
    memo              TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_t_transaction_date       ON dwh.t_transaction (transaction_date);
CREATE INDEX IF NOT EXISTS idx_t_transaction_category   ON dwh.t_transaction (category_id);
CREATE INDEX IF NOT EXISTS idx_t_transaction_account    ON dwh.t_transaction (account_id);
CREATE INDEX IF NOT EXISTS idx_t_transaction_entry_type ON dwh.t_transaction (entry_type);

COMMENT ON TABLE  dwh.t_transaction                  IS '取引明細';
COMMENT ON COLUMN dwh.t_transaction.transaction_date IS '取引日';
COMMENT ON COLUMN dwh.t_transaction.amount           IS '金額（常に正。収支は entry_type で判別）';


-- =========================================
-- マスタ: 年度予算
-- fiscal_year は年番号（例: 2026年 = 2026-01-01 ～ 2026-12-31）
-- =========================================
CREATE TABLE IF NOT EXISTS dwh.m_budget (
    budget_id      INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fiscal_year    INTEGER       NOT NULL,
    annual_budget  NUMERIC(14,2) NOT NULL CHECK (annual_budget >= 0),
    start_month    SMALLINT      NOT NULL DEFAULT 1 CHECK (start_month BETWEEN 1 AND 12),
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT uq_m_budget_fiscal_year_start_month UNIQUE (fiscal_year, start_month)
);

COMMENT ON TABLE dwh.m_budget IS '年度予算マスタ';
COMMENT ON COLUMN dwh.m_budget.fiscal_year IS '年番号（2026年なら 2026）';
COMMENT ON COLUMN dwh.m_budget.annual_budget IS '年予算額';
COMMENT ON COLUMN dwh.m_budget.start_month IS '年度開始月（本アプリ運用は1）';


-- =========================================
-- マスタ: 予算計算から除外するカテゴリ
-- =========================================
CREATE TABLE IF NOT EXISTS dwh.m_budget_excluded_category (
    budget_id     INTEGER NOT NULL REFERENCES dwh.m_budget(budget_id) ON DELETE CASCADE,
    category_id   INTEGER NOT NULL REFERENCES dwh.m_category(category_id) ON DELETE RESTRICT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (budget_id, category_id)
);

COMMENT ON TABLE dwh.m_budget_excluded_category IS '予算進捗の分子計算から除外するカテゴリ';


-- =========================================
-- 初期データ（要件）
-- 2026年予算 = 2,800,000円
-- 除外カテゴリ = 固定費
-- =========================================
INSERT INTO dwh.m_budget (fiscal_year, annual_budget, start_month)
VALUES (2026, 2800000, 1)
ON CONFLICT (fiscal_year, start_month) DO UPDATE
SET annual_budget = EXCLUDED.annual_budget,
    updated_at = now();

INSERT INTO dwh.m_budget_excluded_category (budget_id, category_id)
SELECT b.budget_id, c.category_id
FROM dwh.m_budget b
JOIN dwh.m_category c ON c.category_name = '固定費'
WHERE b.fiscal_year = 2026
  AND b.start_month = 1
ON CONFLICT (budget_id, category_id) DO NOTHING;


-- =========================================
-- ビュー: 予算使用率（当日基準）
-- 分子: 固定費除外後の支出累計
-- 分母: 年予算 / 12 * 経過月数
-- =========================================
CREATE OR REPLACE VIEW dwh.v_budget_usage_rate AS
WITH budget_target AS (
    SELECT
        b.budget_id,
        b.fiscal_year,
        b.annual_budget,
        b.start_month,
        make_date(b.fiscal_year, b.start_month, 1) AS fiscal_start_date
    FROM dwh.m_budget b
    WHERE b.fiscal_year = EXTRACT(YEAR FROM CURRENT_DATE)::int
    LIMIT 1
),
as_of_info AS (
    SELECT
        bt.*,
        CURRENT_DATE AS as_of_date,
        (((EXTRACT(MONTH FROM CURRENT_DATE)::int - bt.start_month + 12) % 12) + 1) AS elapsed_months
    FROM budget_target bt
),
numerator_data AS (
    SELECT
        COALESCE(SUM(t.amount), 0) AS expense_amount_excluding_fixed
    FROM as_of_info ai
    LEFT JOIN dwh.t_transaction t
        ON t.transaction_date BETWEEN ai.fiscal_start_date AND ai.as_of_date
       AND t.entry_type = 'expense'
       AND NOT EXISTS (
           SELECT 1
           FROM dwh.m_budget_excluded_category bec
           WHERE bec.budget_id = ai.budget_id
             AND bec.category_id = t.category_id
       )
)
SELECT
    ai.fiscal_year,
    ai.as_of_date,
    ai.annual_budget,
    ai.elapsed_months,
    nd.expense_amount_excluding_fixed AS numerator,
    (ai.annual_budget / 12.0 * ai.elapsed_months) AS denominator,
    CASE
        WHEN (ai.annual_budget / 12.0 * ai.elapsed_months) = 0 THEN 0
        ELSE ROUND(nd.expense_amount_excluding_fixed / (ai.annual_budget / 12.0 * ai.elapsed_months) * 100.0, 2)
    END AS usage_rate_percent
FROM as_of_info ai
CROSS JOIN numerator_data nd;
