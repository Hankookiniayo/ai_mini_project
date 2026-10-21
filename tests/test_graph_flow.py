"""그래프 전체 흐름 테스트 — MCP를 가짜 객체로 갈아 끼워 오프라인으로 돌린다.

SupervisorDeps에 fake MCP를 '주입'하는 것만으로 서버·MCP 프로세스 없이 그래프 전체를
검증할 수 있다 (교재 14장 의존성 주입의 효과). 데이터는 실제 목업 파일(data/shop/*.json)을 쓴다.
"""
import asyncio

from agent.deps import SupervisorDeps
from agent.fake_llm import FastLLM, QualityLLM
from agent.graph import build_graph
from agent.validators import run_checks
from mcp_tools.policies import PolicySearcher, load_catalog
from mcp_tools.shop import ShopStore
from mcp_tools.shop_repo import JsonShopRepository


class FakeMCP:
    """MCP 대역. low_score_calls: 처음 N번의 정책 검색 점수를 일부러 낮춘다.
    broken_orders: 이 주문번호는 get_order가 예외를 던진다 (조회 장애 재현)."""
    transport = "fake"

    def __init__(self, low_score_calls=0, broken_orders=()):
        self.shop = ShopStore(JsonShopRepository())
        self.searcher = PolicySearcher(load_catalog(), mode="keyword")
        self.low_score_calls = low_score_calls
        self.broken = set(broken_orders)
        self.search_calls: list[str] = []

    async def find_prompt(self, name):
        return "ops"

    async def get_prompt(self, name, args, server="ops"):
        return "1. 주문 확인 2. 정책 확인 3. 안내"

    async def find_tool(self, name):
        return "ops"

    async def find_resource_template(self, name):
        return "ops", "policy://shop/{topic}"

    async def call_tool(self, name, args, server="ops"):
        if name == "list_my_orders":
            return self.shop.list_orders(args["customer_id"])
        if name == "get_order":
            if args["order_id"] in self.broken:
                raise RuntimeError("order DB unreachable")
            return self.shop.get_order(args["customer_id"], args["order_id"])
        if name == "get_tracking":
            return self.shop.get_tracking(args["customer_id"], args["order_id"])
        if name == "search_products":
            return self.shop.search_products(args["query"])
        if name == "search_policies":
            self.search_calls.append(args["query"])
            result = self.searcher.search(args["query"], args.get("top_k", 3))
            if self.low_score_calls > 0:
                self.low_score_calls -= 1
                for h in result["hits"]:
                    h["score"] = 0.01
            return result
        raise ValueError(name)

    async def read_resource(self, uri, server="ops"):
        return self.searcher.by_topic[uri.rsplit("/", 1)[-1]].body


def run(query, mcp=None, customer_id="C-0001"):
    deps = SupervisorDeps(fast_llm=FastLLM("fast", 0.00015),
                          quality_llm=QualityLLM("quality", 0.005),
                          mcp=mcp or FakeMCP(), session_store=None, version="v2")
    return asyncio.run(build_graph(deps).ainvoke(
        {"query": query, "thread_id": "t", "customer_id": customer_id, "history": [],
         "events": [], "cost_usd": 0.0}))


def events(state, kind):
    return [e for e in state["events"] if e["type"] == kind]


def policies_by_key(state):
    out: dict[str, set] = {}
    for c in state["collected"]:
        if c["source"] in ("policy", "policy_ref"):
            out.setdefault(c["key"], set()).add(c["topic"])
    return out


def test_chat_route_skips_pipeline():
    state = run("안녕, 너는 누구야?")
    assert state["route"] == "chat" and "솔방울마켓" in state["final"]
    assert not events(state, "PlanCreated")


def test_delayed_parcel_without_order_id_finds_delayed_order():
    """주문번호 없이 "택배가 안 와요" → 코드 보정이 배송지연 주문을 먼저 찾는다."""
    state = run("택배가 3일째 안 와요")
    plan = events(state, "PlanCreated")[0]
    assert plan["targets"][0] == "ORD-1004"
    assert "delivery-delay" in policies_by_key(state)["ORD-1004"]
    assert "ORD-1004" in state["final"] and "배송지연" in state["final"]


def test_order_id_in_question_is_used_as_is():
    state = run("ORD-1005 바지 사이즈가 안 맞는데 반품돼요?")
    assert events(state, "PlanCreated")[0]["targets"] == ["ORD-1005"]
    assert policies_by_key(state)["ORD-1005"] & {"return-exchange", "size-exchange"}


def test_general_policy_question_needs_no_order():
    state = run("반품 기간이 어떻게 돼요?")
    assert events(state, "PlanCreated")[0]["targets"] == []
    assert "return-exchange" in policies_by_key(state)["질문"]


def test_unknown_order_id_is_reported_not_invented():
    state = run("ORD-9999 주문 어디쯤 왔어요?")
    done = [e for e in events(state, "TaskCompleted") if e["source"] == "order"]
    assert done[0]["found"] is False
    assert "확인되지 않습니다" in state["final"]
    assert events(state, "Validation")[-1]["verdict"] == "합격"


def test_other_customers_order_is_not_visible():
    """목업회원 2(박하늘)가 목업회원 1의 주문번호를 물으면 '없는 주문'과 똑같이 처리된다."""
    state = run("ORD-1004 언제 와요?", customer_id="C-0002")
    done = [e for e in events(state, "TaskCompleted") if e["source"] == "order"]
    assert [(e["target"], e["found"]) for e in done] == [("ORD-1004", False)]
    assert "러닝화" not in state["final"] and "대전 터미널" not in state["final"]


def test_each_customer_plans_from_own_orders():
    """같은 질문이라도 고객마다 자기 주문 중에서 고른다 (회원 1: 지연 주문, 회원 2: 배송중 주문)."""
    assert events(run("택배가 안 와요"), "PlanCreated")[0]["targets"] == ["ORD-1004"]
    assert events(run("택배가 안 와요", customer_id="C-0002"),
                  "PlanCreated")[0]["targets"] == ["ORD-2001"]


def test_low_score_triggers_requery_round():
    mcp = FakeMCP(low_score_calls=1)
    state = run("ORD-1004 언제 와요?", mcp)
    checks = events(state, "ReplanCheck")
    assert [c["is_sufficient"] for c in checks] == [False, False, True]
    assert checks[1]["reason"].startswith("유사도")
    assert len(mcp.search_calls) == 2 and "배송 지연" in mcp.search_calls[1]   # 질의가 바뀜
    assert "delivery-delay" in policies_by_key(state)["ORD-1004"]


def test_failed_task_is_isolated():
    state = run("이어폰이랑 신발 언제 와요?", FakeMCP(broken_orders={"ORD-1003"}))
    failed = events(state, "TaskFailed")
    assert len(failed) == 1 and failed[0]["target"] == "ORD-1003"
    assert "delivery-delay" in policies_by_key(state)["ORD-1004"]   # 나머지는 계속


def test_gate_removes_self_contradicting_analysis_task():
    state = run("ORD-1003 언제 도착해요?")
    assert state["gate_removed"] == 1
    assert events(state, "AnalysisSkipped")


def test_validation_rejects_short_first_draft_then_passes():
    state = run("ORD-1006 환불 언제 돼요?")
    v = events(state, "Validation")
    assert v[0]["verdict"] == "불합격" and v[0]["checks"]["format"] is False
    assert v[0]["missing_orders"] == ["ORD-1006"]
    assert v[-1]["verdict"] == "합격"


class InventingWriter(QualityLLM):
    """항상 정책에 없는 기간("14일")을 지어내는 작성자 — 검수 3회 실패 경로를 재현한다."""

    async def write(self, query, collected, attempt, feedback, on_token=None):
        draft = "1) 주문 확인: ORD-1006 반품 진행 중입니다. 2) 환불은 14일 이내에 됩니다. 3) 기다려 주세요."
        return draft, self._usage(query, draft)


def test_three_failed_checks_fall_back_to_safe_answer():
    """2차 실측 회귀: 3번 반려된 초안을 '통과 처리'하지 않고 안전한 기본 답변으로 바꾼다."""
    deps = SupervisorDeps(fast_llm=FastLLM("fast", 0.00015),
                          quality_llm=InventingWriter("quality", 0.005),
                          mcp=FakeMCP(), session_store=None, version="v2")
    state = asyncio.run(build_graph(deps).ainvoke(
        {"query": "ORD-1006 환불 언제 돼요?", "thread_id": "t", "customer_id": "C-0001",
         "history": [], "events": [], "cost_usd": 0.0}))
    v = events(state, "Validation")
    assert [e["verdict"] for e in v] == ["불합격", "불합격", "안전 답변 대체"]
    assert events(state, "SafeFallback")
    assert "14일" not in state["final"] and "고객센터" in state["final"]
    assert "ORD-1006" in state["final"]                  # 확인된 사실(주문·상태)은 그대로 알려준다
    assert run_checks(state["final"], state["collected"], "ORD-1006 환불 언제 돼요?")["passed"]


def test_price_question_uses_real_product_price():
    """회귀: 가격 질문이 잡담으로 빠져 가격을 지어내던 문제 (화면 24,900원 → 답변 29,900원)."""
    state = run("극세사 담요 얼마에요?")
    assert state["route"] == "order"
    plan = events(state, "PlanCreated")[0]
    assert plan["targets"] == [] and "상품" in plan["reason"]       # 주문은 조회하지 않음
    assert "24,900원" in state["final"]
    assert not events(state, "PolicyMiss")                          # 가격 질문엔 정책이 필요 없음
    assert events(state, "Validation")[-1]["verdict"] == "합격"


def test_unknown_product_price_is_not_invented():
    state = run("노트북 얼마에요?")
    assert "찾지 못했습니다" in state["final"] and "원입니다" not in state["final"]
