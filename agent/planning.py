"""plan 단계의 '코드 보정' — LLM이 세운 계획을 실행 가능한 형태로 고친다 (교재 7장 권위 게이트 정신).

장애 대응 버전에서는 fake/ollama/openai 세 LLM 파일이 같은 보정 코드(_guess_target 등)를
각자 복붙해 들고 있었다. 여기로 한 곳에 모아서 plan_node가 LLM 종류와 상관없이 같은
규칙으로 보정한다 — 그래서 LLM 클래스는 "모델이 뭐라고 했는지(raw)"만 돌려주면 된다.

LLM이 돌려주는 raw 계획 형식
    {"needs_order": bool,        # 특정 주문을 봐야 하는 질문인가 (일반 정책 질문이면 false)
     "needs_analysis": bool,     # 여러 주문을 비교 분석해야 하나
     "tasks": [{"agent": "research", "target": "ORD-1004"}, {"agent": "analysis"}]}

보정 규칙 (위에서부터 우선)
  ① 질문에 주문번호가 있으면 그 주문 — 고객 목록에 없는 번호여도 조회는 한다
     (조회 결과 '없음'을 근거로 "확인되지 않는 주문"이라고 답해야 하기 때문)
  ② LLM이 고른 주문 — 단, 고객 주문 목록에 실제로 있는 번호만 (지어낸 번호는 버림)
  ③ 질문에 나온 상품명·카테고리와 맞는 주문 ("이어폰 언제 와요?" → 이어폰 주문)
  -- 여기서 코드 기준 '일반 정책 질문'("반품 기간이 어떻게 돼요?")이면 주문 조회 없이 정책만 찾는다
  ④ 질문 의도에 맞는 상태이면서 '문제(issue)가 있는' 주문 ("환불 언제 돼요?" → 반품 진행 중 주문)
     — LLM이 일반 질문(needs_order=false)이라고 해도 조회한다
  -- 여기서 LLM이 일반 질문이라고 했으면 주문 조회 없이 정책만 찾는다
  ⑤ 질문 의도에 맞는 상태의 가장 최근 주문, 그것도 없으면 문제 있는 주문 → 가장 최근 주문

  상품 문의("극세사 담요 얼마에요?")면 상품 조회 task를 추가하고, 주문 얘기(주문·배송·반품 등)가
  없으면 주문은 조회하지 않는다 — 담요를 주문한 적이 있어도 '가격'을 물은 것이기 때문.

  [1차 실측 후 수정] 처음에는 LLM의 needs_order=false를 ③·④보다 먼저 믿었다. 그러자 gpt-4o-mini가
  "환불 언제 돼요?"를 일반 질문으로 분류해서, 반품 진행 중인 ORD-1006을 4번의 실행에서 한 번도
  조회하지 않았다. 상품명이 나오거나 의도에 맞는 문제 주문이 있으면 코드가 우선하도록 순서를 바꿨다.
"""
import re

ORDER_ID = re.compile(r"ORD-\d{4}")

# route 단계의 안전망(LLM 출력이 깨졌을 때)과 fake LLM이 함께 쓰는 '쇼핑 상담' 판별 패턴
ORDER_PATTERN = re.compile(
    r"주문|배송|택배|반품|교환|환불|취소|사이즈|도착|배송지|주소|송장|불량|하자|파손|찢어|"
    r"ORD-\d|언제 와|안 와|안와|못 받|얼마|가격|재고|품절|옵션|색상|할인|상품")

# 상품 자체(가격·옵션·재고)를 묻는 질문 — 상품 조회 task를 만든다.
# [추가] 이 경로가 없을 때 "극세사 담요 얼마에요?"가 잡담으로 처리돼 가격이 지어내졌다.
PRODUCT_PATTERN = re.compile(r"얼마|가격|값이|재고|품절|옵션|색상|색깔|무슨 색|할인|사이즈.{0,6}(있|뭐|나와)")

# 특정 '주문'에 대한 얘기인지 — 상품 문의와 섞여 있을 때 주문 조회 여부를 가른다
ORDER_TALK = re.compile(r"주문|배송|택배|반품|교환|환불|취소|도착|언제 와|안 와|안와|배송지|주소|송장|ORD-\d")

# 특정 주문이 아니라 '규정 자체'를 묻는 질문의 단서 (④·⑤ 추측을 하지 않게)
GENERAL_PATTERN = re.compile(
    r"정책|규정|보통|일반적으로|기간이 어떻게|며칠 걸려|며칠 안에|기준이|몇 번|몇 회|무료")

# 질문 의도 → 관련 있을 법한 주문 상태 (규칙 ④)
INTENT_STATUSES = [
    (re.compile(r"안 와|안와|언제 와|도착|배송 조회|택배|늦"), ("delayed", "shipping")),
    (re.compile(r"환불"), ("return_requested", "delivered")),
    (re.compile(r"반품|교환|사이즈|불량|하자|찢어|파손"), ("delivered", "return_requested")),
    (re.compile(r"취소|배송지|주소"), ("paid", "preparing")),
]


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^0-9a-zA-Z가-힣]+", text) if len(w) >= 2]


def _product_match(query: str, order: dict) -> bool:
    """상품명·카테고리 단어 중 하나가 질문에 (조사가 붙은 채로라도) 나오면 같은 주문으로 본다."""
    vocab = set()
    for name in order.get("item_names", []):
        vocab.update(_words(name))
    vocab.update(order.get("categories", []))
    return any(q.startswith(v) for q in _words(query) for v in vocab)


def is_general_question(query: str) -> bool:
    return bool(GENERAL_PATTERN.search(query)) and not ORDER_ID.search(query)


def is_product_question(query: str, raw: dict) -> bool:
    return bool(PRODUCT_PATTERN.search(query)) or bool(raw.get("needs_product"))


def pick_orders(query: str, raw: dict, orders: list[dict]) -> tuple[list[str], str]:
    """보정 규칙 ①~⑤를 적용해 (조회할 주문번호 목록, 어떤 규칙으로 골랐는지)를 돌려준다."""
    known = [o["order_id"] for o in orders]
    mentioned = list(dict.fromkeys(ORDER_ID.findall(query)))                       # ①
    llm_picked = [t.get("target") for t in raw.get("tasks", [])
                  if t.get("agent") == "research" and t.get("target") in known]  # ②
    picked = list(dict.fromkeys(mentioned + llm_picked))
    if picked:
        return picked, "질문의 주문번호" if mentioned and not llm_picked else "계획"
    if is_product_question(query, raw) and not ORDER_TALK.search(query):
        return [], "상품 문의 (주문 조회 안 함)"

    by_product = [o["order_id"] for o in orders if _product_match(query, o)]      # ③
    if by_product:
        return by_product, "상품명 일치"
    if is_general_question(query):
        return [], "일반 정책 질문 (주문 조회 안 함)"

    intent = next(((p, s) for p, s in INTENT_STATUSES if p.search(query)), None)
    matched = [o for o in orders if intent and o["status"] in intent[1]]
    with_issue = [o["order_id"] for o in matched if o.get("issue")]                # ④
    if with_issue:
        return with_issue, f"질문 의도에 맞는 문제 주문({'/'.join(intent[1])})"
    if not raw.get("needs_order", True):
        return [], "일반 정책 질문 (LLM 판단, 주문 조회 안 함)"
    if matched:                                                                     # ⑤
        # 관련 없는 주문까지 끌어오면 답변에 엉뚱한 정책이 섞이므로 가장 최근 1건만 본다
        return [matched[0]["order_id"]], f"질문 의도({'/'.join(intent[1])})"
    issues = [o["order_id"] for o in orders if o.get("issue")]                      # ⑤
    if issues:
        return issues, "문제 있는 주문"
    return known[:1], "최근 주문"


def normalize_plan(query: str, raw: dict, orders: list[dict]) -> dict:
    """raw 계획 → 실행할 task 목록. analysis task는 최대 1개만 남긴다(gate가 다시 거름)."""
    targets, reason = pick_orders(query, raw, orders)
    tasks = [{"id": f"task-{i}", "agent": "research", "kind": "order",
              "target": oid, "status": "pending"} for i, oid in enumerate(targets)]
    if is_product_question(query, raw):   # 가격·옵션은 화면과 같은 상품 데이터에서 조회
        tasks.append({"id": "task-product", "agent": "research", "kind": "product",
                      "target": "상품", "query": query, "status": "pending"})
    if any(t.get("agent") == "analysis" for t in raw.get("tasks", [])):
        tasks.append({"id": "task-analysis", "agent": "analysis",
                      "kind": "order-comparison", "status": "pending"})
    return {"needs_analysis": bool(raw.get("needs_analysis", False)),
            "tasks": tasks, "target_reason": reason}
