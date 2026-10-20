"""validate 규칙 검사 단위 테스트 — 실제 목업 주문(ORD-1004 배송지연)과 정책 발췌로 검사한다."""
from agent.validators import (check_dates, check_numbers, check_order_ids, cited_numbers,
                              month_days, run_checks)
from mcp_tools.policies import load_catalog
from mcp_tools.shop import ShopStore

SHOP = ShopStore()
DELAY_POLICY = {p.topic: p for p in load_catalog()}["delivery-delay"]
COLLECTED = [
    {"source": "order", "data": SHOP.get_order("ORD-1004"),
     "tracking": SHOP.get_tracking("ORD-1004")},
    {"source": "policy", "key": "ORD-1004", "topic": "delivery-delay", "score": 0.8,
     "data": DELAY_POLICY.body},
]


def test_cited_numbers_only_with_units():
    text = "1) ORD-1004 결제 89,000원, 3일째 이동 없음 2) 적립금 2,000원 3) 1~2영업일"
    assert cited_numbers(text) == [89000.0, 3.0, 2000.0, 2.0]   # '1)' 'ORD-1004'는 제외


def test_check_numbers_detects_invented_period():
    facts = "결제 금액 89,000원 / 도착 예정일보다 2일 이상 늦어지면 적립금 2,000원"
    assert check_numbers("89000원 결제, 적립금 2,000원", facts) == []   # 쉼표 표기 차이는 허용
    assert check_numbers("14일 이내에 보상됩니다", facts) == [14.0]    # 정책에 없는 기간


def test_check_order_ids_and_dates():
    facts = "주문 ORD-1004 도착 예정일 2026-10-17"
    assert check_order_ids("ORD-1004와 ORD-1010을 확인했습니다", facts) == ["ORD-1010"]
    assert month_days("10월 17일, 2026-10-18, 10/19") == {(10, 17), (10, 18), (10, 19)}
    assert check_dates("10월 17일 도착 예정이었습니다", facts) == []
    assert check_dates("10월 21일에 도착합니다", facts) == ["10월 21일"]


def test_run_checks_pass_and_fail():
    good = ("1) 주문 확인: ORD-1004(에어라이트 러닝화)는 도착 예정일 10월 17일보다 3일 늦어진 "
            "배송지연 상태입니다. 2) 정책 안내: 2일 이상 지연되면 적립금 2,000원을 드립니다. "
            "3) 다음 단계: 고객센터로 위치 확인을 요청해 주세요.")
    res = run_checks(good, COLLECTED, "택배가 안 와요")
    assert res["passed"], res

    bad = ("1) 주문 ORD-1007이 10월 22일 도착합니다. 2) 지연 보상으로 5,000원을 드립니다. "
           "3) 기다려 주세요.")
    res = run_checks(bad, COLLECTED, "택배가 안 와요")
    assert not res["passed"]
    assert res["checks"] == {"format": True, "numbers": False, "order_ids": False,
                             "dates": False, "coverage": False}
    assert res["unknown_numbers"] == [5000.0]
    assert res["unknown_orders"] == ["ORD-1007"]
    assert res["unknown_dates"] == ["10월 22일"]
    assert res["missing_orders"] == ["ORD-1004"]


def test_basic_mode_checks_format_only():
    bad = "1) 주문 ORD-1007이 10월 22일 도착합니다. 보상으로 5,000원을 드립니다."
    res = run_checks(bad, COLLECTED, "택배가 안 와요", mode="basic")
    assert res["passed"]   # 개선 전 동작 — 형식만 맞으면 환각이 그대로 통과


def test_missing_policy_is_stated_in_facts():
    """2차 실측 회귀: 정책을 끝내 못 찾았으면 그 사실을 작성자에게 명시한다."""
    from agent.facts import NO_POLICY_MARK, format_facts
    collected = [{"source": "policy_miss", "key": "질문", "best": "refund", "score": 0.21},
                 {"source": "policy_miss", "key": "질문", "best": "refund", "score": 0.25}]
    text = format_facts(collected)
    assert text.count(NO_POLICY_MARK) == 1 and "추측" in text
    # 재검색으로 결국 찾았으면 '못 찾음'을 쓰지 않는다
    found = collected + [{"source": "policy", "key": "질문", "topic": "refund", "score": 0.5,
                          "data": "# 환불\n1. 검수 완료 후 3영업일 이내"}]
    assert NO_POLICY_MARK not in format_facts(found)


def test_day_span_must_match_a_span_not_a_date():
    """3차 수정 회귀: 회수일 2026-10-14의 '14' 때문에 '14일 이내'가 통과하던 문제."""
    facts = "회수 완료 2026-10-14, 도착 예정일 2026-10-17 / 검수 완료 후 3영업일 이내, 수령 후 7일 이내"
    assert check_numbers("환불은 14일 이내에 됩니다", facts) == [14.0]      # 기간 → 기간 숫자와만 비교
    assert check_numbers("3영업일 이내, 7일 이내", facts) == []
    assert check_numbers("17일 도착 예정이었습니다", facts) == []            # 그냥 'N일'은 날짜로도 허용


def test_wrong_product_price_is_rejected():
    """회귀: 화면 가격과 다른 가격을 말하면 반려된다."""
    from agent.facts import format_facts
    facts = format_facts([{"source": "product", "data": SHOP.search_products("극세사 담요 얼마에요?")}])
    assert "24,900원" in facts
    assert check_numbers("극세사 담요는 24,900원입니다", facts) == []
    assert check_numbers("극세사 담요는 29,900원입니다", facts) == [29900.0]
