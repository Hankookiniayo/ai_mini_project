"""실제 LLM(openai_llm.py / ollama_llm.py)이 함께 쓰는 시스템 프롬프트와 응답 파싱.

장애 대응 버전에서는 두 파일이 같은 프롬프트 문자열을 각자 들고 있어서 한쪽만 고치는
실수가 생기기 쉬웠다. 프롬프트·파싱을 여기 한 곳에 두고, 두 파일은 '어떤 API를
어떻게 부르는지'만 다르게 한다.
"""
import json

from .planning import ORDER_PATTERN

ROUTE_SYSTEM = (
    "당신은 쇼핑몰 '솔방울마켓' 고객상담 라우터다. 사용자 메시지를 분류하라. "
    "주문·배송·택배·반품·교환·환불·취소·배송지 변경·상품 하자, 그리고 상품 가격·옵션·재고 문의 등 "
    "쇼핑 상담이면 route=order, "
    "인사·잡담·자기소개·챗봇에 대한 질문이면 route=chat. "
    "needs_analysis는 여러 주문을 서로 비교해야 할 때만 true."
)

PLAN_SYSTEM = (
    "당신은 쇼핑몰 상담 Supervisor의 planning 노드다. 상담 표준 절차에 따라 실행 계획(tasks)을 세워라. "
    "아래 '고객 주문 목록' 중 질문과 관련된 주문마다 agent=research, target=주문번호 task를 하나씩 만들어라. "
    "질문에 주문번호가 없으면 상품명·카테고리·상태로 관련 주문을 골라라 "
    "(예: '택배가 안 와요' → 배송중/배송지연 주문, '환불 언제 돼요' → 반품요청됨 주문). "
    "특정 주문이 아니라 규정 자체를 묻는 일반 질문이면 needs_order=false, tasks=[]로 답하라. "
    "상품의 가격·옵션·재고를 묻는 질문이면 needs_product=true (주문 얘기가 없으면 needs_order=false). "
    "목록에 없는 주문번호를 지어내지 마라. "
    "여러 주문을 비교 분석해야 하면 agent=analysis task를 추가해도 된다."
)

PLAN_JSON_HINT = (
    'JSON 객체로만 응답하라. 스키마: {"needs_order": boolean, "needs_product": boolean, "needs_analysis": boolean, '
    '"tasks": [{"agent": "research"|"analysis", "target": "주문번호(research일 때)"}]}'
)
ROUTE_JSON_HINT = (
    'JSON 객체로만 응답하라. 스키마: {"route": "chat" 또는 "order", "needs_analysis": true 또는 false}'
)

ANALYZE_SYSTEM = (
    "당신은 쇼핑몰 주문 분석가다. 여러 주문의 상태를 비교해 공통 원인이 의심되는 패턴"
    "(예: 같은 택배사 주문이 동시에 지연)이 있는지 한두 문장으로 짧게 답하라. "
    "패턴이 없으면 '특이 패턴 없음'이라고만 답하라."
)

CHAT_SYSTEM = (
    "당신은 쇼핑몰 '솔방울마켓'의 친절한 상담 챗봇이다. 한두 문장으로 간결하게 답하라. "
    "상품 가격·재고·주문 상태 같은 구체적인 값은 절대 추측하지 말고, 상품명과 함께 다시 물어봐 "
    "달라고 안내하라. "
    "주문·배송·반품 문의를 도울 수 있다고 안내하라. "
    "대화 이력에서 사용자가 밝힌 이름이 있으면 정확히 기억해 답하라."
)

WRITE_SYSTEM = (
    "당신은 쇼핑몰 '솔방울마켓'의 고객상담원이다. '수집된 사실'만 사용해 친절하고 간결한 "
    "한국어로 답하라. 반드시 아래 번호 형식으로 답하라 (번호는 항상 1)부터).\n"
    "1) 확인 내용: 주문 문의면 주문번호와 현재 상태, 근거가 되는 날짜·금액. "
    "상품 문의면 상품명과 판매가 (상품 ID는 쓰지 않는다). "
    "주문도 상품도 조회하지 않았으면 '특정 주문 없이 정책 기준으로 안내드립니다'라고 쓴다\n"
    "2) 안내: 주문 문의면 정책 발췌에 근거한 기준(기간·비용 등), 상품 문의면 선택할 수 있는 옵션\n"
    "3) 다음 단계: 고객이 직접 할 수 있는 일 (주문 관련 신청은 마이페이지 > 주문 내역 또는 "
    "고객센터, 구매는 상품 페이지에서)\n"
    "규칙: 주문번호·날짜·금액·기간은 수집된 값을 그대로 인용하라. 정책 발췌에 없는 기간이나 "
    "금액은 절대 쓰지 마라. 조회한 주문은 주문번호를 모두 언급하라. 조회되지 않은 주문은 "
    "확인되지 않았다고 밝혀라. 가격은 상품 정보의 판매가만 쓰고, 상품을 찾지 못했으면 가격을 말하지 마라. "
    "챗봇은 반품·취소를 직접 접수할 수 없으니 접수했다고 말하지 마라. "
    "사실에 '정책 문서를 찾지 못함'이 있으면 기간·금액·조건을 추측하지 말고, 정확한 기준은 "
    "고객센터 1:1 문의로 안내한다고 써라."
)


def render_orders(orders: list[dict]) -> str:
    """plan 프롬프트에 넣을 고객 주문 목록 (MCP list_my_orders 결과)."""
    if not orders:
        return "(주문 없음)"
    return "\n".join(
        f"- {o['order_id']} | {o['status_label']} | {', '.join(o['item_names'])} "
        f"| {', '.join(o.get('categories', []))} | 주문일 {o['ordered_at']}"
        + (f" | 문제: {o['issue']}" if o.get("issue") else "")
        for o in orders)


def plan_user_message(query: str, procedure: str, orders: list[dict]) -> str:
    return (f"## 상담 표준 절차\n{procedure}\n\n## 고객 주문 목록\n{render_orders(orders)}"
            f"\n\n## 사용자 질문\n{query}")


def parse_route(content: str, query: str) -> dict:
    """구조화 출력이 깨지거나 값이 엉뚱하면 정규식으로 폴백한다 (모델 출력을 그대로 믿지 않음)."""
    try:
        raw = json.loads(content)
        route = raw.get("route") if raw.get("route") in ("chat", "order") else None
        if route:
            return {"route": route, "needs_analysis": bool(raw.get("needs_analysis", False))}
    except (json.JSONDecodeError, AttributeError):
        pass
    return {"route": "order" if ORDER_PATTERN.search(query) else "chat",
            "needs_analysis": False}


def parse_plan(content: str) -> dict:
    """raw 계획만 파싱한다 — 대상 주문 보정은 planning.normalize_plan이 한다."""
    try:
        raw = json.loads(content)
        if isinstance(raw, dict):
            raw.setdefault("tasks", [])
            return raw
    except json.JSONDecodeError:
        pass
    return {"needs_order": True, "needs_analysis": False, "tasks": []}


def write_user_message(query: str, facts: str, feedback: str | None) -> str:
    user = f"## 사용자 질문\n{query}\n\n## 수집된 사실\n{facts}"
    if feedback:
        user += f"\n\n## 이전 초안에 대한 검증 반려 사유 (반드시 반영)\n{feedback}"
    return user


def analyze_facts(collected: list[dict]) -> str:
    orders = [c["data"] for c in collected
              if c["source"] == "order" and c["data"].get("found", True)]
    return "\n".join(
        f"- {o['order_id']}: 상태={o['status_label']}, 택배사 {o.get('carrier') or '-'}, "
        f"문제 {o.get('issue') or '없음'}" for o in orders) or "(조회한 주문 없음)"
