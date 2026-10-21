"""쇼핑몰 데이터 접근 계층 (Repository) — '어디에 저장돼 있는지'는 이 파일 안에만 있다.

mcp_tools/shop.py의 ShopStore(판정 로직: 지연·반품 기간 계산 등)는 아래 메서드만 부른다.
저장소를 JSON 파일 → SQLite → (나중에) Postgres·주문 API로 바꿔도 ShopStore와 MCP Tool,
화면 API, 에이전트 코드는 그대로다.

    meta()                          {"as_of", "store_name"}
    find_login(login_id)            {"customer_id", "password_hash"} | None
    get_customer(customer_id)       고객 정보 | None
    list_products()                 상품 목록
    list_orders(customer_id)        그 고객의 주문 전체 (items·tracking·return 포함)
    get_order(customer_id, order_id) 그 고객의 주문 1건 | None — 남의 주문이면 '없음'과 똑같이 None

구현
  SqliteShopRepository : 기본값. data/shop/schema.sql 스키마의 SQLite 파일(SHOP_DB_PATH, 기본 ./shop.db)
  JsonShopRepository   : data/shop/*.json을 직접 읽는다 (DB 시드 원본 · 단위 테스트용 고정 데이터)
  SHOP_BACKEND=sqlite(기본) | json 으로 고른다.

두 구현은 같은 모양의 dict를 돌려준다 — tests/test_shop_store.py가 두 저장소에서 같은 테스트를 돌려 확인한다.
"""
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Protocol

ROOT = Path(__file__).resolve().parent.parent
SHOP_DIR = ROOT / "data" / "shop"
SCHEMA_PATH = SHOP_DIR / "schema.sql"
DEFAULT_DB_PATH = ROOT / "shop.db"

# 주문 dict의 키 순서 — JSON 원본과 SQLite 복원 결과를 똑같이 맞춘다
_ORDER_COLUMNS = ("order_id", "customer_id", "ordered_at", "status")
_ORDER_TAIL = ("shipping_fee", "total_amount", "payment_method", "shipping_address", "carrier",
               "tracking_no", "expected_delivery", "delivered_at")
_RETURN_COLUMNS = ("requested_at", "reason", "pickup_done_at", "inspection",
                   "return_shipping_fee", "expected_refund_amount")
_CUSTOMER_COLUMNS = ("customer_id", "name", "grade", "points", "default_address")


def hash_password(password: str, salt: str) -> str:
    """PBKDF2-SHA256. 목업이라 salt는 customer_id를 쓴다 (실서비스는 사용자마다 난수 salt를 따로 저장)."""
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return digest.hex()


class ShopRepository(Protocol):
    def meta(self) -> dict: ...
    def find_login(self, login_id: str) -> dict | None: ...
    def get_customer(self, customer_id: str) -> dict | None: ...
    def list_products(self) -> list[dict]: ...
    def list_orders(self, customer_id: str) -> list[dict]: ...
    def get_order(self, customer_id: str, order_id: str) -> dict | None: ...


# ── JSON 파일 ────────────────────────────────────────────────────────
class JsonShopRepository:
    """data/shop/*.json을 호출할 때마다 다시 읽는다 — 파일이 작아서 비용이 거의 없고,
    고정 데이터를 고치면 재시작 없이 바로 반영된다. 비밀번호가 평문으로 들어 있는 건 목업이라서다
    (SQLite에는 해시만 들어간다)."""

    def __init__(self, shop_dir: Path = SHOP_DIR):
        self.shop_dir = shop_dir

    def _load(self, name: str) -> dict:
        return json.loads((self.shop_dir / name).read_text(encoding="utf-8"))

    def meta(self) -> dict:
        data = self._load("customers.json")
        return {"as_of": data["as_of"], "store_name": data["store_name"]}

    def _customers(self) -> list[dict]:
        return self._load("customers.json")["customers"]

    def find_login(self, login_id: str) -> dict | None:
        c = next((c for c in self._customers() if c["login_id"] == login_id), None)
        if c is None:
            return None
        return {"customer_id": c["customer_id"],
                "password_hash": hash_password(c["password"], c["customer_id"])}

    def get_customer(self, customer_id: str) -> dict | None:
        c = next((c for c in self._customers() if c["customer_id"] == customer_id), None)
        return {k: c[k] for k in _CUSTOMER_COLUMNS} if c else None

    def list_products(self) -> list[dict]:
        return self._load("products.json")["products"]

    def list_orders(self, customer_id: str) -> list[dict]:
        return [o for o in self._load("orders.json")["orders"] if o["customer_id"] == customer_id]

    def get_order(self, customer_id: str, order_id: str) -> dict | None:
        return next((o for o in self.list_orders(customer_id) if o["order_id"] == order_id), None)


# ── SQLite ───────────────────────────────────────────────────────────
def seed_sqlite(db_path: Path, source: JsonShopRepository | None = None) -> None:
    """schema.sql로 테이블을 만들고 JSON 고정 데이터를 넣는다. 이미 있는 행은 건너뛴다
    (INSERT OR IGNORE) — 서버와 MCP 프로세스가 동시에 시드해도 결과가 같다."""
    src = source or JsonShopRepository()
    raw_customers = src._customers()
    orders = src._load("orders.json")["orders"]
    with closing(sqlite3.connect(db_path, timeout=10)) as conn:
        conn.execute("BEGIN IMMEDIATE")   # 동시에 시드하는 다른 프로세스는 여기서 기다린다
        for stmt in SCHEMA_PATH.read_text(encoding="utf-8").split(";"):
            if stmt.strip():
                conn.execute(stmt)
        conn.executemany("INSERT OR IGNORE INTO shop_meta VALUES (?, ?)", src.meta().items())
        conn.executemany(
            "INSERT OR IGNORE INTO customers VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(c["customer_id"], c["login_id"], hash_password(c["password"], c["customer_id"]),
              c["name"], c["grade"], c["points"], c["default_address"]) for c in raw_customers])
        conn.executemany(
            "INSERT OR IGNORE INTO products VALUES (?, ?, ?, ?, ?, ?)",
            [(p["product_id"], p["name"], p["category"], p["price"],
              json.dumps(p["options"], ensure_ascii=False), p.get("emoji"))
             for p in src.list_products()])
        for o in orders:
            conn.execute(
                f"INSERT OR IGNORE INTO orders VALUES ({', '.join('?' * 12)})",
                [o[k] for k in _ORDER_COLUMNS + _ORDER_TAIL])
            conn.executemany(
                "INSERT OR IGNORE INTO order_items VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(o["order_id"], n, i["product_id"], i["name"],
                  json.dumps(i["option"], ensure_ascii=False), i["qty"], i["price"])
                 for n, i in enumerate(o["items"])])
            conn.executemany(
                "INSERT OR IGNORE INTO tracking_events VALUES (?, ?, ?, ?, ?)",
                [(o["order_id"], n, e["time"], e["location"], e["status"])
                 for n, e in enumerate(o["tracking"])])
            if o.get("return"):
                conn.execute("INSERT OR IGNORE INTO returns VALUES (?, ?, ?, ?, ?, ?, ?)",
                             [o["order_id"]] + [o["return"].get(k) for k in _RETURN_COLUMNS])
        conn.commit()


class SqliteShopRepository:
    """쿼리마다 연결을 새로 연다 — FastAPI 스레드풀·MCP 서버 어디서 불러도 안전하고,
    로컬 파일 SQLite라 연결 비용이 거의 없다. 모든 주문 조회에 customer_id 조건이 붙는다.

    DB가 비어 있으면(처음 실행) JSON 고정 데이터로 자동 시드한다. 다시 만들려면
    python scripts/seed_shop_db.py --reset"""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        with closing(self._connect()) as conn:
            seeded = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orders'").fetchone()
        if not seeded:
            seed_sqlite(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def meta(self) -> dict:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT key, value FROM shop_meta").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def find_login(self, login_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT customer_id, password_hash FROM customers WHERE login_id = ?",
                               (login_id,)).fetchone()
        return dict(row) if row else None

    def get_customer(self, customer_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(f"SELECT {', '.join(_CUSTOMER_COLUMNS)} FROM customers "
                               "WHERE customer_id = ?", (customer_id,)).fetchone()
        return dict(row) if row else None

    def list_products(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM products ORDER BY product_id").fetchall()
        return [{**dict(r), "options": json.loads(r["options"])} for r in rows]

    def _orders(self, where: str, params: tuple) -> list[dict]:
        """주문 + 품목 + 배송 추적 + 반품을 JSON 원본과 같은 모양의 dict로 묶는다."""
        with closing(self._connect()) as conn:
            orders = conn.execute(f"SELECT * FROM orders WHERE {where} ORDER BY order_id",
                                  params).fetchall()
            ids = [o["order_id"] for o in orders]
            if not ids:
                return []
            marks = ", ".join("?" * len(ids))
            items = conn.execute(f"SELECT * FROM order_items WHERE order_id IN ({marks}) "
                                 "ORDER BY order_id, line_no", ids).fetchall()
            events = conn.execute(f"SELECT * FROM tracking_events WHERE order_id IN ({marks}) "
                                  "ORDER BY order_id, seq", ids).fetchall()
            returns = conn.execute(f"SELECT * FROM returns WHERE order_id IN ({marks})",
                                   ids).fetchall()
        result = []
        for o in orders:
            oid = o["order_id"]
            order = {k: o[k] for k in _ORDER_COLUMNS}
            order["items"] = [{"product_id": i["product_id"], "name": i["name"],
                               "option": json.loads(i["option"]), "qty": i["qty"],
                               "price": i["price"]} for i in items if i["order_id"] == oid]
            order.update({k: o[k] for k in _ORDER_TAIL})
            order["tracking"] = [{"time": e["time"], "location": e["location"],
                                  "status": e["status"]} for e in events if e["order_id"] == oid]
            ret = next((r for r in returns if r["order_id"] == oid), None)
            if ret:
                order["return"] = {k: ret[k] for k in _RETURN_COLUMNS}
            result.append(order)
        return result

    def list_orders(self, customer_id: str) -> list[dict]:
        return self._orders("customer_id = ?", (customer_id,))

    def get_order(self, customer_id: str, order_id: str) -> dict | None:
        found = self._orders("customer_id = ? AND order_id = ?", (customer_id, order_id))
        return found[0] if found else None


def repository_from_env() -> ShopRepository:
    """SHOP_BACKEND=sqlite(기본) | json. sqlite면 SHOP_DB_PATH(기본 ./shop.db)."""
    if os.environ.get("SHOP_BACKEND", "sqlite") == "json":
        return JsonShopRepository()
    return SqliteShopRepository(os.environ.get("SHOP_DB_PATH", DEFAULT_DB_PATH))
