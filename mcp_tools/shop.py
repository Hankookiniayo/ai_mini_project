"""쇼핑몰 목업 데이터 저장소 — 주문 조회 MCP Tool과 화면용 API가 '같은 파일'을 읽는다.

데이터 위치: data/shop/
  customer.json  로그인한 목업 고객 1명 + 기준일(as_of)
  products.json  상품 목록 (화면 상품 그리드용)
  orders.json    주문 6건 (상태별로 하나씩) + 배송 추적 이벤트

왜 한 곳에서 읽나
  화면에 "배송지연"으로 보이는 주문을 챗봇에 물으면, 챗봇도 '그 주문의 실제 값'으로
  답해야 한다. 화면(server.py의 /api/shop/*)과 에이전트(MCP Tool)가 이 클래스 하나를
  거쳐 같은 JSON을 읽게 해서 둘이 어긋날 수 없게 했다.

왜 기준일(as_of)을 고정하나
  "3일째 안 와요" 같은 경과일 계산을 실제 오늘 날짜로 하면, 평가를 다른 날 돌릴 때마다
  정답이 바뀐다. 목업 데이터 안에 '오늘'을 적어 두면 언제 돌려도 같은 결과가 나온다.

에이전트(agent/)는 이 모듈을 import하지 않는다 — MCP Tool 결과(JSON)로만 데이터를 받는다.
"""
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

SHOP_DIR = Path(__file__).resolve().parent.parent / "data" / "shop"

STATUS_LABELS = {
    "paid": "결제완료",
    "preparing": "배송준비중",
    "shipping": "배송중",
    "delayed": "배송지연",
    "delivered": "배송완료",
    "return_requested": "반품요청됨",
}

RETURN_WINDOW_DAYS = 7          # 교환·반품 신청 가능 기간 (data/shop/policies/return-exchange.md와 같은 값)
DELAY_THRESHOLD_DAYS = 2        # 도착 예정일보다 이만큼 늦으면 '지연' (delivery-delay.md와 같은 값)
_ORDER_ID = re.compile(r"^ORD-\d{4}$")   # 입력 검증 — 이상한 문자열로 파일을 뒤지지 않게


def _day(text: str | None) -> date | None:
    """'2026-10-17' 또는 '2026-10-17 10:20' → date. 값이 없으면 None."""
    if not text:
        return None
    return datetime.strptime(text[:10], "%Y-%m-%d").date()


class ShopStore:
    def __init__(self, shop_dir: Path = SHOP_DIR):
        self.shop_dir = shop_dir

    # ── 파일 읽기 ─────────────────────────────────────────────────────
    # 호출할 때마다 다시 읽는다 — 파일이 작아서 비용이 거의 없고, 목업 데이터를 고치면
    # 서버를 재시작하지 않아도 화면과 챗봇에 바로 반영된다.
    def _load(self, name: str) -> dict:
        return json.loads((self.shop_dir / name).read_text(encoding="utf-8"))

    def customer(self) -> dict:
        data = self._load("customer.json")
        return {**data["customer"], "as_of": data["as_of"], "store_name": data["store_name"]}

    def as_of(self) -> date:
        return _day(self._load("customer.json")["as_of"])

    def products(self) -> list[dict]:
        return self._load("products.json")["products"]

    def _orders(self) -> list[dict]:
        return self._load("orders.json")["orders"]

    def _find(self, order_id: str) -> dict | None:
        return next((o for o in self._orders() if o["order_id"] == order_id), None)

    # ── 판정 로직 ─────────────────────────────────────────────────────
    def issue(self, order: dict) -> str | None:
        """상담이 필요한 '문제'가 있는 주문이면 한 줄 설명, 아니면 None.

        상태값(status)만 믿지 않고 날짜로도 계산한다 — status가 '배송중'이어도 예정일을
        DELAY_THRESHOLD_DAYS 이상 넘겼으면 지연으로 본다."""
        today = self.as_of()
        if order["status"] == "return_requested":
            ret = order.get("return") or {}
            return f"반품 진행 중 (회수 완료 {ret.get('pickup_done_at', '-')}, {ret.get('inspection', '-')})"
        expected = _day(order.get("expected_delivery"))
        not_delivered = order.get("delivered_at") is None and order["status"] in (
            "shipping", "delayed")
        if not_delivered and expected and (today - expected).days >= DELAY_THRESHOLD_DAYS:
            last = order["tracking"][-1] if order["tracking"] else None
            idle = (today - _day(last["time"])).days if last else None
            where = f", 마지막 이동 {last['time']} {last['location']}({idle}일째 이동 없음)" if last else ""
            return f"배송지연: 도착 예정일 {order['expected_delivery']}보다 {(today - expected).days}일 지남{where}"
        return None

    def _return_info(self, order: dict) -> dict:
        """배송완료 주문의 '수령 후 경과일'과 반품 신청 마감일."""
        delivered = _day(order.get("delivered_at"))
        if not delivered:
            return {}
        elapsed = (self.as_of() - delivered).days
        deadline = delivered + timedelta(days=RETURN_WINDOW_DAYS)
        return {"days_since_delivery": elapsed,
                "return_deadline": deadline.isoformat(),
                "return_window_open": self.as_of() <= deadline}

    # ── MCP Tool / 화면 API가 쓰는 공개 메서드 ─────────────────────────
    def summary(self, order: dict) -> dict:
        category = {p["product_id"]: p["category"] for p in self.products()}
        return {
            "order_id": order["order_id"],
            "ordered_at": order["ordered_at"],
            "status": order["status"],
            "status_label": STATUS_LABELS[order["status"]],
            "item_names": [i["name"] for i in order["items"]],
            # "신발 언제 와요?"처럼 카테고리로 묻는 경우를 plan 보정에서 찾기 위함
            "categories": sorted({category.get(i["product_id"], "") for i in order["items"]} - {""}),
            "total_amount": order["total_amount"],
            "issue": self.issue(order),
        }

    def list_orders(self) -> dict:
        """최근 주문이 먼저 오도록 정렬한 주문 요약 목록."""
        orders = sorted(self._orders(), key=lambda o: o["ordered_at"], reverse=True)
        return {"as_of": self.as_of().isoformat(),
                "orders": [self.summary(o) for o in orders]}

    def get_order(self, order_id: str) -> dict:
        order = self._find(order_id) if _ORDER_ID.match(order_id or "") else None
        if order is None:
            return {"found": False, "order_id": order_id,
                    "message": f"주문번호 {order_id}를 찾을 수 없습니다"}
        detail = {k: v for k, v in order.items() if k != "tracking"}   # 추적은 get_tracking으로
        return {"found": True, **detail, "status_label": STATUS_LABELS[order["status"]],
                "issue": self.issue(order), "as_of": self.as_of().isoformat(),
                **self._return_info(order)}

    def get_tracking(self, order_id: str) -> dict:
        order = self._find(order_id) if _ORDER_ID.match(order_id or "") else None
        if order is None:
            return {"found": False, "order_id": order_id,
                    "message": f"주문번호 {order_id}를 찾을 수 없습니다"}
        events = order["tracking"]
        last = events[-1] if events else None
        return {"found": True, "order_id": order_id, "carrier": order.get("carrier"),
                "tracking_no": order.get("tracking_no"), "events": events,
                "last_event": last,
                "days_since_last_event": (self.as_of() - _day(last["time"])).days if last else None}

    def search_products(self, query: str) -> dict:
        """상품명·카테고리 단어가 질문에 나오는 상품을 찾는다 (가격·옵션 문의용).

        [추가] 처음에는 상품 조회 Tool이 없어서 "극세사 담요 얼마에요?"가 잡담으로 처리됐고,
        모델이 가격을 지어냈다 (화면 24,900원 → 답변 29,900원). 화면과 같은 products.json에서
        실제 가격을 읽어 오게 한다. 한국어 조사를 흡수하려고 '질문 단어가 상품 단어로 시작하면'
        일치로 본다 (planning.py의 상품명 매칭과 같은 방식)."""
        words = [w for w in re.split(r"[^0-9a-zA-Z가-힣]+", query) if len(w) >= 2]
        found = []
        for p in self.products():
            vocab = {w for w in re.split(r"[^0-9a-zA-Z가-힣]+", p["name"]) if len(w) >= 2}
            if any(q.startswith(v) for q in words for v in vocab):
                found.append(p)
        if not found:   # 상품명이 없으면 카테고리로 ("신발 뭐 있어요?")
            found = [p for p in self.products()
                     if any(q.startswith(p["category"]) for q in words)]
        return {"query": query, "found": bool(found),
                "products": [{k: v for k, v in p.items() if k != "emoji"} for p in found]}

    def orders_for_screen(self) -> list[dict]:
        """화면의 '내 주문' 패널용 — 요약 + 상세 + 추적을 한 번에."""
        orders = sorted(self._orders(), key=lambda o: o["ordered_at"], reverse=True)
        return [{**o, **self.summary(o), **self._return_info(o)} for o in orders]
