"""쇼핑몰 주문·배송 상담 Supervisor — langgraph.StateGraph.

route → (chat 직행 | order 상담 파이프라인)
order: plan(MCP Prompt 절차 + 고객 주문 목록) → gate(권위 게이트) → research(주문 조회·정책 검색,
       병렬 + 에러 격리 + 재시도) → replan_check(정책 문서가 부족하면 질의를 바꿔 재검색)
       → analysis(needs_analysis=True일 때만) → write → validate(반려 시 재작성) → finalize

모든 노드는 SupervisorDeps를 functools.partial로 주입받는 순수 함수다 (교재 14장 의존성 주입).
각 노드는 @traceable로 LangSmith에 계층 트리로 기록된다.

장애 대응 버전과의 대응 관계
  모니터링 조회(get_monitoring_status) → 주문 조회(get_order + get_tracking)
  runbook 검색(search_runbooks)        → 정책 문서 검색(search_policies)
  "이상 시스템마다 runbook 확보"        → "조회한 주문마다(일반 질문이면 질문 1건) 정책 문서 확보"
  validate: 수치·시스템 누락           → 수치·주문번호·날짜·주문 누락
"""
import asyncio
import os
import time
from functools import partial
from typing import TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from langsmith import traceable

from .deps import SupervisorDeps
from .facts import safe_answer
from .planning import ORDER_ID, normalize_plan
from .retry import with_retry
from .validators import run_checks

MAX_REPLAN_ROUNDS = 2       # 1라운드: 질문 + 주문 상황으로 검색, 2라운드: 상태 기반 표준 문구로 재검색
MAX_POLICIES_PER_KEY = 2    # "사이즈가 안 맞는데 반품돼요?"처럼 두 정책(반품·사이즈 교환)이 걸리는 질문 대비
# 2등 문서는 1등 점수의 이 비율 이상일 때만 함께 가져온다. 임베딩 점수는 0.4~0.6에 몰려 있어서
# 0.8로 두면 거의 모든 2등이 따라 들어왔다 (실측: 배송 지연 질문에 배송 조회 안내까지 섞임).
POLICY_SECOND_RATIO = float(os.environ.get("POLICY_SECOND_RATIO", "0.9"))
# 유사도 임계치 — 검색 방식마다 점수 분포가 달라 따로 둔다.
# hybrid·벡터 값은 dev 질문(eval/retrieval_dev.json)에서 정확도가 가장 높은 구간의 가운데로 정했다
# (scripts/compare_retrieval.py — hybrid-large 0.12~0.18 → 0.15, vector-large 0.17~0.28 → 0.26).
# 키워드 0.3은 1차 측정 질문을 보고 정한 값이라 그대로 둔다 (Before 비교 기준을 바꾸지 않기 위해).
POLICY_MIN_SCORE_HYBRID = float(os.environ.get("POLICY_MIN_SCORE_HYBRID", "0.15"))
POLICY_MIN_SCORE = float(os.environ.get("POLICY_MIN_SCORE", "0.26"))
POLICY_MIN_SCORE_KEYWORD = float(os.environ.get("POLICY_MIN_SCORE_KEYWORD", "0.3"))
MAX_VALIDATION_ATTEMPTS = 3
GENERAL_KEY = "질문"         # 특정 주문 없이 정책만 묻는 질문일 때 정책을 묶는 키

# 2라운드 재검색에 쓰는 '상태별 표준 문구' — 사용자의 표현이 정책 문서와 너무 다를 때 대비
STATUS_HINTS = {
    "paid": "주문 취소 배송지 변경",
    "preparing": "주문 취소 배송지 변경 출고 전",
    "shipping": "배송 조회 언제 도착 송장",
    "delayed": "배송 지연 택배가 안 와요 보상",
    "delivered": "교환 반품 신청 수령 후",
    "return_requested": "반품 환불 처리 기간 검수",
}


class AgentState(TypedDict, total=False):
    query: str
    thread_id: str
    customer_id: str          # 로그인 세션에서 서버가 채운다 — 주문 조회 Tool에 그대로 넘긴다
    history: list[dict]
    route: str
    needs_analysis: bool
    tasks: list[dict]
    gate_removed: int
    collected: list[dict]
    replan_rounds: int
    retry_count: int
    parallel_elapsed: float
    draft: str
    validation_attempts: int
    validation_feedback: str
    final: str
    safe_fallback: bool
    last_prompt: str
    cost_usd: float
    chat_served_by: str
    events: list[dict]


def _cost(state: AgentState, usage: dict) -> float:
    return round(state.get("cost_usd", 0.0) + usage["usd"], 6)


@traceable(name="route_node")
async def route_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """모든 질문이 무거운 상담 파이프라인을 탈 필요는 없다 — 인사·잡담은 chat으로 직행."""
    result, usage = deps.fast_llm.route(state["query"])
    event = {"type": "RouteDecided", "route": result["route"]}
    return {"route": result["route"], "needs_analysis": result["needs_analysis"],
            "cost_usd": _cost(state, usage),
            "events": state.get("events", []) + [event]}


@traceable(name="chat_answer_node")
async def chat_answer_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """direct 경로 — 히스토리 기반 응답 (세션 기억 증명 지점).

    토큰 단위 스트리밍 — get_stream_writer()는 /chat/stream처럼 custom 모드로 소비될
    때만 실제로 전송되고, /chat(ainvoke)처럼 스트리밍하지 않는 호출에서는 자동으로
    no-op이 된다."""
    writer = get_stream_writer()
    reply, usage = await deps.fast_llm.chat(
        state["query"], state.get("history", []),
        on_token=lambda piece: writer({"type": "AnswerToken", "delta": piece}))
    served_by = usage.get("served_by", getattr(deps.fast_llm, "name", None))
    return {"final": reply, "last_prompt": usage.get("prompt"),
            "cost_usd": _cost(state, usage), "chat_served_by": served_by,
            "events": state["events"] + [{"type": "Finalize", "pipeline": "chat-direct",
                                          "served_by": served_by}]}


@traceable(name="plan_node")
async def plan_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Plan/Task 분해.

    1) MCP Tool list_my_orders로 로그인한 고객의 주문 요약을 먼저 받는다 — LLM이 '실제로 있는 주문'
       중에서 고르게 하기 위함 (에이전트는 데이터를 직접 읽지 않고 MCP로만 받는다)
    2) MCP Prompt cs_consult_prompt가 상담 표준 절차를 깔아준다
    3) LLM이 raw 계획을 세우고, planning.normalize_plan이 코드로 보정한다
    tool/prompt를 어느 서버가 들고 있는지 미리 가정하지 않고 find_*로 먼저 찾는다."""
    query = state["query"]
    orders_server = await deps.mcp.find_tool("list_my_orders")
    order_list = await deps.mcp.call_tool("list_my_orders", {"customer_id": state["customer_id"]},
                                          server=orders_server)
    orders = order_list.get("orders", [])

    prompt_server = await deps.mcp.find_prompt("cs_consult_prompt")
    mentioned = ORDER_ID.findall(query)
    procedure = await deps.mcp.get_prompt(
        "cs_consult_prompt",
        {"order_id": ", ".join(mentioned) or "(질문에 주문번호 없음)", "question": query},
        server=prompt_server)

    raw, usage = deps.fast_llm.plan(query, procedure, orders)
    result = normalize_plan(query, raw, orders)
    targets = [t["target"] for t in result["tasks"] if t.get("kind") == "order"]
    event = {"type": "PlanCreated", "task_count": len(result["tasks"]),
             "targets": targets, "reason": result["target_reason"]}
    return {"tasks": result["tasks"], "needs_analysis": result["needs_analysis"],
            "cost_usd": _cost(state, usage), "events": state["events"] + [event]}


@traceable(name="gate_node")
async def gate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """권위 게이트 — needs_analysis=False인데 섞여 들어온 analysis task를
    프롬프트가 아니라 코드로 강제 제거한다."""
    tasks = state["tasks"]
    if not state.get("needs_analysis", False):
        kept = [t for t in tasks if t["agent"] != "analysis"]
        removed = len(tasks) - len(kept)
    else:
        kept, removed = tasks, 0
    event = {"type": "GateApplied", "removed_analysis_tasks": removed}
    return {"tasks": kept, "gate_removed": removed,
            "events": state["events"] + [event]}


@traceable(name="research_node")
async def research_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """가드 + 병렬 실행 + 재시도 + MCP Tool/Resource 실호출.

    task 종류
      order         : MCP Tool get_order + get_tracking (두 호출도 동시에)
      product       : MCP Tool search_products (가격·옵션 — 화면과 같은 데이터)
      policy_search : MCP Tool search_policies → 점수가 임계치 이상인 상위 문서를 Resource로 읽기
    """
    tasks = state["tasks"]
    research_indices = [i for i, t in enumerate(tasks)
                        if t["agent"] == "research" and t["status"] == "pending"]
    # 객체가 아니라 '실행할 목록'을 검사한다 — 할 일이 없으면 건너뛴다
    if not research_indices:
        return {"events": state["events"] +
                [{"type": "ResearchSkipped", "reason": "pending research task 0개"}]}

    retry_total = state.get("retry_count", 0)

    # URI·서버를 코드에 하드코딩하지 않는다 — 연결된 서버들을 먼저 조회(list)해서
    # resource template / tool을 들고 있는 서버를 찾은 뒤에만 요청한다.
    kinds = {tasks[i]["kind"] for i in research_indices}
    order_server = policy_server = policy_uri_template = search_server = product_server = None
    if "order" in kinds:
        order_server = await deps.mcp.find_tool("get_order")
    if "product" in kinds:
        product_server = await deps.mcp.find_tool("search_products")
    if "policy_search" in kinds:
        search_server = await deps.mcp.find_tool("search_policies")
        policy_server, policy_uri_template = await deps.mcp.find_resource_template("get_policy")

    async def fetch_order(idx: int, task: dict) -> dict:
        # customer_id는 LLM 계획이 아니라 세션에서 온 값 — 남의 주문번호를 물어도 '없음'으로 돌아온다
        args = {"customer_id": state["customer_id"], "order_id": task["target"]}
        (detail, retries), tracking = await asyncio.gather(
            with_retry(deps.mcp.call_tool, "get_order", args, server=order_server),
            deps.mcp.call_tool("get_tracking", args, server=order_server))
        return {"idx": idx, "source": "order", "data": detail, "tracking": tracking,
                "retries": retries}

    async def search_policy(idx: int, task: dict) -> dict:
        res = await deps.mcp.call_tool("search_policies",
                                       {"query": task["query"], "top_k": 3},
                                       server=search_server)
        hits = res.get("hits", [])
        method = res.get("method") or ""
        threshold = (POLICY_MIN_SCORE_KEYWORD if method.startswith("keyword")
                     else POLICY_MIN_SCORE_HYBRID if method == "hybrid"
                     else POLICY_MIN_SCORE)
        best = hits[0] if hits else None
        base = {"idx": idx, "key": task["key"], "query": task["query"], "method": method,
                "retries": 0}
        if best is None or best["score"] < threshold:
            return {**base, "source": "policy_miss",
                    "best": best and best["topic"], "score": best and best["score"]}
        # 1등과 점수가 비슷한(POLICY_SECOND_RATIO 이상) 2등까지 함께 가져온다 — 복합 질문 대비
        chosen = [h for h in hits[:MAX_POLICIES_PER_KEY]
                  if h["score"] >= threshold and h["score"] >= best["score"] * POLICY_SECOND_RATIO]
        docs = []
        for h in chosen:
            uri = policy_uri_template.format(topic=h["topic"])
            text = await deps.mcp.read_resource(uri, server=policy_server)
            if not text.lstrip().startswith("# Policy Not Found"):
                docs.append({"topic": h["topic"], "score": h["score"], "data": text})
        if not docs:   # 검색은 됐는데 본문이 없음
            return {**base, "source": "policy_miss", "best": best["topic"],
                    "score": best["score"]}
        return {**base, "source": "policy", "docs": docs}

    async def run_one(idx: int) -> dict:
        task = tasks[idx]
        # 에러 격리 — 예외를 이 코루틴 안에서 흡수해야 gather 전체가 죽지 않는다
        try:
            await asyncio.sleep(0.35)   # 태스크당 고정 지연 — 병렬 효과를 실측하기 위함
            if task["kind"] == "order":
                return await fetch_order(idx, task)
            if task["kind"] == "product":
                data = await deps.mcp.call_tool("search_products", {"query": task["query"]},
                                                server=product_server)
                return {"idx": idx, "source": "product", "data": data, "retries": 0}
            return await search_policy(idx, task)
        except Exception as exc:
            return {"idx": idx, "source": "error", "retries": 0,
                    "error": f"{type(exc).__name__}: {exc}"}

    started = time.perf_counter()
    results = await asyncio.gather(*(run_one(i) for i in research_indices))
    elapsed = round(time.perf_counter() - started, 2)

    new_tasks = [dict(t) for t in tasks]
    collected = list(state.get("collected", []))
    events = list(state["events"])
    fetched_topics = {c["topic"] for c in collected if c["source"] == "policy"}
    for r in results:
        task = new_tasks[r["idx"]]
        retry_total += r["retries"]
        src = r["source"]
        if src == "error":
            task["status"] = "failed"
            collected.append({"source": "error", "target": task.get("target"),
                              "error": r["error"]})
            events.append({"type": "TaskFailed", "id": task["id"],
                           "target": task.get("target"), "error": r["error"]})
            continue
        task["status"] = "done"
        if src == "order":
            collected.append({"source": "order", "data": r["data"], "tracking": r["tracking"]})
            events.append({"type": "TaskCompleted", "id": task["id"], "source": "order",
                           "target": task["target"], "found": r["data"].get("found", True),
                           "status": r["data"].get("status_label"),
                           "retries": r["retries"]})
        elif src == "product":
            collected.append({"source": "product", "data": r["data"]})
            events.append({"type": "TaskCompleted", "id": task["id"], "source": "product",
                           "target": ", ".join(p["name"] for p in r["data"]["products"])
                                     or "일치하는 상품 없음",
                           "found": r["data"]["found"], "retries": 0})
        elif src == "policy":
            for doc in r["docs"]:
                if doc["topic"] in fetched_topics:   # 다른 주문과 같은 정책 → 본문은 한 번만
                    collected.append({"source": "policy_ref", "key": r["key"],
                                      "topic": doc["topic"], "score": doc["score"]})
                else:
                    fetched_topics.add(doc["topic"])
                    collected.append({"source": "policy", "key": r["key"], "data": doc["data"],
                                      "topic": doc["topic"], "score": doc["score"],
                                      "method": r["method"]})
                events.append({"type": "TaskCompleted", "id": task["id"], "source": "policy",
                               "target": doc["topic"], "key": r["key"], "score": doc["score"],
                               "method": r["method"], "retries": 0})
        else:   # policy_miss
            collected.append({"source": "policy_miss", "key": r["key"],
                              "best": r.get("best"), "score": r.get("score")})
            events.append({"type": "PolicyMiss", "id": task["id"], "key": r["key"],
                           "best": r.get("best"), "score": r.get("score"),
                           "method": r.get("method")})
    return {"tasks": new_tasks, "collected": collected, "retry_count": retry_total,
            "parallel_elapsed": elapsed, "events": events}


def _looked_up(collected: list[dict]) -> dict[str, dict]:
    """조회에 성공한 주문만 {주문번호: 상세}로 모은다."""
    found: dict[str, dict] = {}
    for c in collected:
        if c["source"] == "order" and c["data"].get("found", True):
            found[c["data"]["order_id"]] = c["data"]
    return found


def policy_query(user_query: str, order: dict | None, attempt: int) -> str:
    """정책 문서 검색 질의.
    1라운드: 사용자 질문 + 주문 상황 — 같은 "반품돼요?"라도 배송완료 주문과 반품진행 주문은
             필요한 정책이 다르므로 주문 상태를 근거로 붙인다.
    2라운드: 1라운드 점수가 임계치 미만이면, 사용자 표현 대신 '상태별 표준 문구'로 재검색한다
             (사용자 말투가 정책 문서 표현과 너무 다른 경우 대비)."""
    if order is None:   # 일반 정책 질문
        return user_query if attempt == 0 else f"{user_query} 기간 비용 신청 방법"
    situation = order["status_label"] + (f", {order['issue']}" if order.get("issue") else "")
    if attempt == 0:
        return f"{user_query}\n주문 상황: {situation}"
    return f"{STATUS_HINTS.get(order['status'], '')} {situation}"


@traceable(name="replan_check_node")
async def replan_check_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """수집 결과가 '충분한지' 실제로 판정한다.

    충분 조건: 조회한 주문마다 유사도 임계치 이상인 정책 문서가 있다.
      - 조회한 주문이 없으면(일반 정책 질문, 또는 없는 주문번호) 질문 1건에 대해 정책을 찾는다.
    부족하면 빠진 주문에 대해서만 policy_search task를 추가한다 (기존 task는 건드리지 않음).
    MAX_REPLAN_ROUNDS에 닿으면 부족해도 강제로 다음 단계로 간다 (무한 루프 가드)."""
    collected = state.get("collected", [])
    rounds = state.get("replan_rounds", 0)
    orders = _looked_up(collected)
    product_only = not orders and any(t.get("kind") == "product" for t in state["tasks"])
    # 주문 없이 가격·옵션만 묻는 질문은 정책 문서가 필요 없다 (상품 데이터로 답한다)
    needs: list[tuple[str, dict | None]] = (
        list(orders.items()) or ([] if product_only else [(GENERAL_KEY, None)]))
    covered = {c["key"] for c in collected if c["source"] in ("policy", "policy_ref")}
    missing = [(key, order) for key, order in needs if key not in covered]

    if not missing or rounds >= MAX_REPLAN_ROUNDS:
        verdict = {"type": "ReplanCheck", "round": rounds, "is_sufficient": True,
                   "covered": sorted(covered), "forced": bool(missing),
                   "missing": [key for key, _ in missing]}
        return {"events": state["events"] + [verdict]}

    gap_tasks = [{"id": f"task-pol{rounds}-{key}", "agent": "research",
                  "kind": "policy_search", "target": key, "key": key,
                  "query": policy_query(state["query"], order, rounds),
                  "status": "pending"} for key, order in missing]
    verdict = {"type": "ReplanCheck", "round": rounds, "is_sufficient": False,
               "missing": [key for key, _ in missing],
               "gap_tasks": [t["id"] for t in gap_tasks],
               "reason": "정책 문서 미확보" if rounds == 0 else "유사도 임계치 미달 → 질의 변경"}
    return {"tasks": state["tasks"] + gap_tasks, "replan_rounds": rounds + 1,
            "events": state["events"] + [verdict]}


@traceable(name="analysis_node")
async def analysis_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """needs_analysis=True가 정당한 경우(여러 주문을 비교해야 하는 질문)를 위한 실행 경로.
    gate가 살려둔 agent=='analysis' task가 있을 때만 수집된 주문들을 비교 분석한다."""
    tasks = state["tasks"]
    pending = [t for t in tasks if t["agent"] == "analysis" and t["status"] == "pending"]
    if not pending:
        return {"events": state["events"] +
                [{"type": "AnalysisSkipped", "reason": "analysis task 0개"}]}
    summary, usage = deps.fast_llm.analyze(state.get("collected", []))
    new_tasks = [dict(t) for t in tasks]
    for t in new_tasks:
        if t["agent"] == "analysis" and t["status"] == "pending":
            t["status"] = "done"
    collected = state.get("collected", []) + [{"source": "analysis", "data": summary}]
    event = {"type": "AnalysisCompleted", "length": len(summary)}
    return {"tasks": new_tasks, "collected": collected,
            "cost_usd": _cost(state, usage), "events": state["events"] + [event]}


@traceable(name="write_node")
async def write_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """고품질 모델로 상담 답변 작성.

    토큰마다 attempt 번호를 같이 실어 보낸다 — validate가 반려해 재작성(attempt 증가)
    될 때 프론트가 새 말풍선으로 끊어서 보여줄 수 있게 하기 위함."""
    attempt = state.get("validation_attempts", 0) + 1
    writer = get_stream_writer()
    draft, usage = await deps.quality_llm.write(
        state["query"], state.get("collected", []),
        attempt, state.get("validation_feedback"),
        on_token=lambda piece: writer(
            {"type": "AnswerToken", "attempt": attempt, "delta": piece}))
    return {"draft": draft, "last_prompt": usage.get("prompt"),
            "cost_usd": _cost(state, usage),
            "events": state["events"] + [{"type": "DraftWritten", "attempt": attempt,
                                          "length": len(draft)}]}


@traceable(name="validate_node")
async def validate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """코드 기준 검증 (형식 · 수치 · 주문번호 · 날짜 · 주문 누락) — agent/validators.py.
    VALIDATION_MODE=basic이면 형식만 본다 (Before/After 비교용).

    MAX_VALIDATION_ATTEMPTS번 모두 반려되면 마지막 초안을 내보내지 않고 '안전한 기본 답변'
    (facts.safe_answer — 수집된 사실만으로 만든 답 + 고객센터 안내)으로 바꾼다.
    [2차 실측 후 수정] 예전에는 '통과 처리'해서 지어낸 기간이 든 답이 2건 그대로 나갔다."""
    attempts = state.get("validation_attempts", 0) + 1
    mode = os.environ.get("VALIDATION_MODE", "rules")
    collected = state.get("collected", [])
    result = run_checks(state["draft"], collected, state["query"], mode=mode)
    ok = result["passed"]
    verdict = "합격" if ok else "불합격"
    fallback = None
    if not ok and attempts >= MAX_VALIDATION_ATTEMPTS:   # 무한 재작성 방지 + 틀린 답 차단
        ok, verdict = True, "안전 답변 대체"
        fallback = safe_answer(collected)
    event = {"type": "Validation", "attempt": attempts, "verdict": verdict, "mode": mode,
             "checks": result["checks"],
             "unknown_numbers": result["unknown_numbers"],
             "unknown_orders": result["unknown_orders"],
             "unknown_dates": result["unknown_dates"],
             "missing_orders": result["missing_orders"]}
    events = state["events"] + [event]
    update = {"validation_attempts": attempts,
              "validation_feedback": None if ok else result["feedback"]}
    if fallback is not None:
        update["draft"] = fallback
        update["safe_fallback"] = True
        events.append({"type": "SafeFallback", "attempts": attempts,
                       "reason": result["feedback"]})
    return {**update, "events": events}


@traceable(name="finalize_node")
async def finalize_node(state: AgentState, deps: SupervisorDeps) -> dict:
    return {"final": state["draft"],
            "events": state["events"] + [{"type": "Finalize", "pipeline": "order-v2"}]}


def _after_route(state: AgentState) -> str:
    return "chat_answer" if state["route"] == "chat" else "plan"


def _after_replan_check(state: AgentState) -> str:
    last = state["events"][-1]
    return "analysis" if last.get("is_sufficient") else "research"


def _after_validate(state: AgentState) -> str:
    return "finalize" if state.get("validation_feedback") is None else "write"


def build_graph(deps: SupervisorDeps):
    g = StateGraph(AgentState)
    node = lambda fn: partial(fn, deps=deps)   # 명시적 의존성 주입
    g.add_node("route", node(route_node))
    g.add_node("chat_answer", node(chat_answer_node))
    g.add_node("plan", node(plan_node))
    g.add_node("gate", node(gate_node))
    g.add_node("research", node(research_node))
    g.add_node("replan_check", node(replan_check_node))
    g.add_node("analysis", node(analysis_node))
    g.add_node("write", node(write_node))
    g.add_node("validate", node(validate_node))
    g.add_node("finalize", node(finalize_node))

    g.set_entry_point("route")
    g.add_conditional_edges("route", _after_route,
                            {"chat_answer": "chat_answer", "plan": "plan"})
    g.add_edge("chat_answer", END)
    g.add_edge("plan", "gate")
    g.add_edge("gate", "research")
    g.add_edge("research", "replan_check")
    g.add_conditional_edges("replan_check", _after_replan_check,
                            {"analysis": "analysis", "research": "research"})
    g.add_edge("analysis", "write")
    g.add_edge("write", "validate")
    g.add_conditional_edges("validate", _after_validate,
                            {"finalize": "finalize", "write": "write"})
    g.add_edge("finalize", END)
    return g.compile()
