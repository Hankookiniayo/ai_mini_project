"""plan 코드 보정(agent/planning.py) 단위 테스트 — LLM 없이 규칙 ①~⑤를 확인한다."""
from agent.planning import normalize_plan, pick_orders
from mcp_tools.shop import ShopStore
from mcp_tools.shop_repo import JsonShopRepository

ORDERS = ShopStore(JsonShopRepository()).list_orders("C-0001")["orders"]   # 목업회원 1의 주문
EMPTY = {"needs_order": True, "tasks": []}


def test_order_id_in_query_wins_even_if_unknown():
    assert pick_orders("ORD-9999 어디쯤 왔어요?", EMPTY, ORDERS)[0] == ["ORD-9999"]


def test_llm_invented_order_is_dropped():
    raw = {"needs_order": True, "tasks": [{"agent": "research", "target": "ORD-7777"},
                                          {"agent": "research", "target": "ORD-1003"}]}
    assert pick_orders("이어폰 언제 와요", raw, ORDERS)[0] == ["ORD-1003"]


def test_product_and_category_match():
    assert pick_orders("이어폰 언제 와요?", EMPTY, ORDERS)[0] == ["ORD-1003"]
    assert pick_orders("신발이 안 와요", EMPTY, ORDERS)[0] == ["ORD-1004"]


def test_intent_prefers_order_with_issue():
    assert pick_orders("택배가 안 와요", EMPTY, ORDERS)[0] == ["ORD-1004"]
    assert pick_orders("환불 언제 돼요?", EMPTY, ORDERS)[0] == ["ORD-1006"]
    # 문제 있는 주문이 없으면 그 상태의 가장 최근 주문 1건
    assert pick_orders("주소 바꾸고 싶어요", EMPTY, ORDERS)[0] == ["ORD-1001"]


def test_general_question_skips_order_lookup():
    targets, reason = pick_orders("반품 기간이 어떻게 돼요?", EMPTY, ORDERS)
    assert targets == [] and "일반" in reason


def test_normalize_keeps_one_analysis_task():
    raw = {"needs_order": True, "needs_analysis": False,
           "tasks": [{"agent": "analysis"}, {"agent": "analysis"}]}
    tasks = normalize_plan("ORD-1003 언제 와요", raw, ORDERS)["tasks"]
    assert [t["agent"] for t in tasks] == ["research", "analysis"]


def test_problem_order_beats_llm_general_label():
    """1차 실측 회귀: gpt-4o-mini가 '환불 언제 돼요?'를 일반 질문(needs_order=false)으로 분류해도
    반품 진행 중인 주문이 있으면 코드가 그 주문을 조회한다."""
    llm_says_general = {"needs_order": False, "tasks": []}
    assert pick_orders("환불 언제 돼요?", llm_says_general, ORDERS)[0] == ["ORD-1006"]
    assert pick_orders("가디건 배송지 바꾸고 싶어요", llm_says_general, ORDERS)[0] == ["ORD-1002"]
    # 문제 주문이 없는 의도면 LLM의 '일반 질문' 판단을 따른다
    assert pick_orders("부분 취소하면 배송비가 붙나요?", llm_says_general, ORDERS)[0] == []


def test_rule_questions_are_general():
    assert pick_orders("사이즈 교환은 몇 번까지 무료예요?", EMPTY, ORDERS)[0] == []



def test_price_question_adds_product_task_without_orders():
    plan = normalize_plan("극세사 담요 얼마에요?", EMPTY, ORDERS)
    assert [t["kind"] for t in plan["tasks"]] == ["product"]
    assert "상품" in plan["target_reason"]
    # 주문 얘기가 섞이면 주문도 조회한다
    plan = normalize_plan("담요 주문한 거 얼마였는지랑 언제 오는지 알려줘", EMPTY, ORDERS)
    assert {t["kind"] for t in plan["tasks"]} == {"order", "product"}
