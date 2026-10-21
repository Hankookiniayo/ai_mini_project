"""쇼핑몰 SQLite DB 만들기 — data/shop/schema.sql 스키마에 data/shop/*.json 고정 데이터를 넣는다.

서버가 처음 뜰 때 DB가 비어 있으면 자동으로 시드하므로 보통은 돌릴 필요가 없다.
JSON 고정 데이터를 고쳤거나 DB를 처음 상태로 되돌리고 싶을 때 --reset으로 다시 만든다.

    python scripts/seed_shop_db.py            # 없으면 만들고, 있으면 빠진 행만 채운다
    python scripts/seed_shop_db.py --reset    # 지우고 새로 만든다
    SHOP_DB_PATH=/tmp/shop.db python scripts/seed_shop_db.py
"""
import argparse
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp_tools.shop_repo import DEFAULT_DB_PATH, seed_sqlite  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="seed shop SQLite DB from data/shop/*.json")
    parser.add_argument("--reset", action="store_true", help="기존 DB 파일을 지우고 새로 만든다")
    args = parser.parse_args()
    db_path = Path(os.environ.get("SHOP_DB_PATH", DEFAULT_DB_PATH))
    if args.reset and db_path.exists():
        db_path.unlink()
    seed_sqlite(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("customers", "products", "orders", "order_items",
                            "tracking_events", "returns")}
    print(f"{db_path}: " + ", ".join(f"{t} {n}" for t, n in counts.items()))


if __name__ == "__main__":
    main()
