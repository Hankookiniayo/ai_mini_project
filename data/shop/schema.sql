-- 쇼핑몰 DB 스키마 (SQLite) — mcp_tools/shop_repo.py의 seed_sqlite()가 이 파일로 테이블을 만들고
-- data/shop/*.json을 넣는다. Postgres로 옮길 때도 쓸 수 있게 SQLite 전용 문법은 피했다
-- (JSON 값은 TEXT 컬럼에 문자열로 둔다).

-- 기준일(as_of)·가게 이름처럼 값이 하나뿐인 설정
CREATE TABLE IF NOT EXISTS shop_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    customer_id     TEXT PRIMARY KEY,
    login_id        TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,          -- PBKDF2-SHA256 (평문 비밀번호는 저장하지 않는다)
    name            TEXT NOT NULL,
    grade           TEXT,
    points          INTEGER NOT NULL DEFAULT 0,
    default_address TEXT
);

CREATE TABLE IF NOT EXISTS products (
    product_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    category   TEXT,
    price      INTEGER NOT NULL,
    options    TEXT NOT NULL DEFAULT '{}',  -- {"size": [...], "color": [...]}
    emoji      TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    order_id          TEXT PRIMARY KEY,
    customer_id       TEXT NOT NULL REFERENCES customers(customer_id),
    ordered_at        TEXT NOT NULL,
    status            TEXT NOT NULL,
    shipping_fee      INTEGER NOT NULL DEFAULT 0,
    total_amount      INTEGER NOT NULL,
    payment_method    TEXT,
    shipping_address  TEXT,
    carrier           TEXT,
    tracking_no       TEXT,
    expected_delivery TEXT,
    delivered_at      TEXT
);
-- '내 주문' 목록은 항상 고객 단위로 최근 순 조회한다
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id, ordered_at);

-- 이름·가격은 주문 당시 값을 복사해 둔다 — 나중에 상품 가격이 바뀌어도 지난 주문 금액은 그대로여야 한다
CREATE TABLE IF NOT EXISTS order_items (
    order_id   TEXT NOT NULL REFERENCES orders(order_id),
    line_no    INTEGER NOT NULL,
    product_id TEXT REFERENCES products(product_id),
    name       TEXT NOT NULL,
    option     TEXT NOT NULL DEFAULT '{}',
    qty        INTEGER NOT NULL,
    price      INTEGER NOT NULL,
    PRIMARY KEY (order_id, line_no)
);

CREATE TABLE IF NOT EXISTS tracking_events (
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    seq      INTEGER NOT NULL,
    time     TEXT NOT NULL,
    location TEXT,
    status   TEXT,
    PRIMARY KEY (order_id, seq)
);

CREATE TABLE IF NOT EXISTS returns (
    order_id               TEXT PRIMARY KEY REFERENCES orders(order_id),
    requested_at           TEXT,
    reason                 TEXT,
    pickup_done_at         TEXT,
    inspection             TEXT,
    return_shipping_fee    INTEGER,
    expected_refund_amount INTEGER
);
