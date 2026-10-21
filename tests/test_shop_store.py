"""주문 조회 서비스(mcp_tools/shop.py) 단위 테스트 — 기준일 2026-10-20 고정.

같은 테스트를 두 저장소(JSON 고정 데이터 / 그 데이터로 시드한 SQLite)에서 똑같이 돌린다 —
저장소를 바꿔도 결과가 같아야 ShopStore·MCP Tool·화면이 그대로 동작한다.
"""
import pytest

from mcp_tools.shop import ShopStore
from mcp_tools.shop_repo import JsonShopRepository, SqliteShopRepository

SOL, HANEUL = "C-0001", "C-0002"   # 목업회원 1, 2


@pytest.fixture(params=["json", "sqlite"])
def shop(request, tmp_path):
    if request.param == "json":
        return ShopStore(JsonShopRepository())
    return ShopStore(SqliteShopRepository(tmp_path / "shop.db"))   # 빈 DB → 자동 시드


def test_list_orders_recent_first_with_issues(shop):
    listing = shop.list_orders(SOL)
    ids = [o["order_id"] for o in listing["orders"]]
    assert listing["as_of"] == "2026-10-20"
    assert ids == ["ORD-1001", "ORD-1002", "ORD-1003", "ORD-1004", "ORD-1005", "ORD-1006"]
    issues = {o["order_id"]: o["issue"] for o in listing["orders"] if o["issue"]}
    assert set(issues) == {"ORD-1004", "ORD-1006"}
    assert "3일째 이동 없음" in issues["ORD-1004"]


def test_shipping_order_on_expected_day_is_not_delayed(shop):
    assert shop.get_order(SOL, "ORD-1003")["issue"] is None   # 예정일 당일 — 아직 지연 아님


def test_delivered_order_return_window(shop):
    o = shop.get_order(SOL, "ORD-1005")
    assert o["days_since_delivery"] == 7
    assert o["return_deadline"] == "2026-10-20" and o["return_window_open"] is True
    late = shop.get_order(HANEUL, "ORD-2002")   # 수령 11일째 — 반품 기간 지남
    assert late["days_since_delivery"] == 11 and late["return_window_open"] is False


def test_return_info_survives_storage(shop):
    ret = shop.get_order(SOL, "ORD-1006")["return"]
    assert ret["expected_refund_amount"] == 53000 and ret["inspection"] == "검수 중"


def test_tracking_days_since_last_event(shop):
    t = shop.get_tracking(SOL, "ORD-1004")
    assert t["last_event"]["location"] == "대전 터미널"
    assert t["days_since_last_event"] == 3


def test_unknown_or_malformed_order_id(shop):
    assert shop.get_order(SOL, "ORD-9999")["found"] is False
    assert shop.get_order(SOL, "../customer")["found"] is False   # 형식 검증
    assert shop.get_tracking(SOL, "ORD-9999")["found"] is False


def test_customers_see_only_their_own_orders(shop):
    sol = {o["order_id"] for o in shop.list_orders(SOL)["orders"]}
    haneul = {o["order_id"] for o in shop.list_orders(HANEUL)["orders"]}
    assert haneul == {"ORD-2001", "ORD-2002", "ORD-2003"}
    assert not sol & haneul
    # 남의 주문번호는 '없는 주문'과 똑같은 응답 — 존재 여부도 드러내지 않는다
    assert shop.get_order(HANEUL, "ORD-1004") == {
        "found": False, "order_id": "ORD-1004", "message": "주문번호 ORD-1004를 찾을 수 없습니다"}
    assert shop.get_tracking(SOL, "ORD-2001")["found"] is False
    assert {o["customer_id"] for o in shop.orders_for_screen(HANEUL)} == {HANEUL}


def test_authenticate(shop):
    assert shop.authenticate("sol", "mock1234")["customer_id"] == SOL
    assert shop.authenticate("haneul", "mock5678")["name"] == "박하늘"
    assert shop.authenticate("sol", "mock5678") is None      # 남의 비밀번호
    assert shop.authenticate("nobody", "mock1234") is None
    assert "password" not in shop.customer(SOL) and "password_hash" not in shop.customer(SOL)


def test_screen_orders_include_tracking_and_summary(shop):
    first = shop.orders_for_screen(SOL)[0]
    assert {"tracking", "status_label", "issue", "items"} <= set(first)


def test_search_products_by_name_and_category(shop):
    assert [x["price"] for x in shop.search_products("극세사 담요 얼마에요?")["products"]] == [24900]
    assert [x["name"] for x in shop.search_products("핸드크림 얼마에요?")["products"]] == ["핸드크림 3종 세트"]
    assert [x["name"] for x in shop.search_products("신발 뭐 있어요?")["products"]] == ["에어라이트 러닝화"]
    assert shop.search_products("노트북 얼마에요?") == {"query": "노트북 얼마에요?", "found": False, "products": []}


def test_json_and_sqlite_return_identical_orders(tmp_path):
    j, s = JsonShopRepository(), SqliteShopRepository(tmp_path / "shop.db")
    for cid in (SOL, HANEUL):
        assert j.list_orders(cid) == s.list_orders(cid)
    assert j.list_products() == s.list_products()
