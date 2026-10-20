"""목업 주문 저장소(mcp_tools/shop.py) 단위 테스트 — 기준일 2026-10-20 고정."""
from mcp_tools.shop import ShopStore

SHOP = ShopStore()


def test_list_orders_recent_first_with_issues():
    listing = SHOP.list_orders()
    ids = [o["order_id"] for o in listing["orders"]]
    assert listing["as_of"] == "2026-10-20"
    assert ids == ["ORD-1001", "ORD-1002", "ORD-1003", "ORD-1004", "ORD-1005", "ORD-1006"]
    issues = {o["order_id"]: o["issue"] for o in listing["orders"] if o["issue"]}
    assert set(issues) == {"ORD-1004", "ORD-1006"}
    assert "3일째 이동 없음" in issues["ORD-1004"]


def test_shipping_order_on_expected_day_is_not_delayed():
    assert SHOP.get_order("ORD-1003")["issue"] is None   # 예정일 당일 — 아직 지연 아님


def test_delivered_order_return_window():
    o = SHOP.get_order("ORD-1005")
    assert o["days_since_delivery"] == 7
    assert o["return_deadline"] == "2026-10-20" and o["return_window_open"] is True


def test_tracking_days_since_last_event():
    t = SHOP.get_tracking("ORD-1004")
    assert t["last_event"]["location"] == "대전 터미널"
    assert t["days_since_last_event"] == 3


def test_unknown_or_malformed_order_id():
    assert SHOP.get_order("ORD-9999")["found"] is False
    assert SHOP.get_order("../customer")["found"] is False   # 형식 검증
    assert SHOP.get_tracking("ORD-9999")["found"] is False


def test_screen_orders_include_tracking_and_summary():
    first = SHOP.orders_for_screen()[0]
    assert {"tracking", "status_label", "issue", "items"} <= set(first)


def test_search_products_by_name_and_category():
    assert [x["price"] for x in SHOP.search_products("극세사 담요 얼마에요?")["products"]] == [24900]
    assert [x["name"] for x in SHOP.search_products("핸드크림 얼마에요?")["products"]] == ["핸드크림 3종 세트"]
    assert [x["name"] for x in SHOP.search_products("신발 뭐 있어요?")["products"]] == ["에어라이트 러닝화"]
    assert SHOP.search_products("노트북 얼마에요?") == {"query": "노트북 얼마에요?", "found": False, "products": []}
