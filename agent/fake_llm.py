"""결정론적 Fake LLM 2종 (fast/quality) + 비용 계산.

실제 LLM 호출 없이도 파이프라인 전체가 결정론적으로 재현되도록, 프롬프트 내용에 따라
정해진 응답을 돌려준다. 각 호출은 LangSmith에 run_type="llm"으로 기록되고, 문자열
길이 기반 토큰 추정치로 비용을 계산해 상태에 누적한다 (모델 분리 효과 비교용).
"""
import re

from langsmith import traceable

from .facts import format_facts
from .planning import GENERAL_PATTERN, ORDER_ID, ORDER_PATTERN, PRODUCT_PATTERN


class FakeLLM:
    def __init__(self, name: str, usd_per_1k_tokens: float):
        self.name = name
        self.usd_per_1k = usd_per_1k_tokens

    def _usage(self, prompt: str, completion: str) -> dict:
        tokens = (len(prompt) + len(completion)) // 4 + 1
        return {"model": self.name, "tokens": tokens,
                "usd": round(tokens / 1000 * self.usd_per_1k, 6)}


class FastLLM(FakeLLM):
    """분류·계획 등 짧고 정형적인 단계 전담 (저비용)."""

    @traceable(run_type="llm", name="fast_llm.route")
    def route(self, query: str) -> tuple[dict, dict]:
        """쇼핑 상담이면 order 파이프라인, 아니면 chat 직행."""
        result = {"route": "order" if ORDER_PATTERN.search(query) else "chat",
                  "needs_analysis": False}
        return result, self._usage(query, str(result))

    @traceable(run_type="llm", name="fast_llm.plan")
    def plan(self, query: str, procedure_prompt: str, orders: list[dict]) -> tuple[dict, dict]:
        """질문에 적힌 주문번호만 task로 만든다 — 나머지(상품명·의도로 주문 찾기)는
        planning.normalize_plan의 코드 보정이 맡는다.

        권위 게이트 재현: needs_analysis=False라고 스스로 판단해 놓고도 analysis task를
        끼워 넣는 '모델의 자기모순'을 결정론적으로 재현한다 → gate 노드가 걸러낸다."""
        tasks = [{"agent": "research", "target": oid}
                 for oid in dict.fromkeys(ORDER_ID.findall(query))]
        tasks.append({"agent": "analysis"})   # 자기모순 task
        result = {"needs_order": not GENERAL_PATTERN.search(query),
                  "needs_product": bool(PRODUCT_PATTERN.search(query)),
                  "needs_analysis": False, "tasks": tasks}
        return result, self._usage(query + procedure_prompt + str(orders), str(result))

    @traceable(run_type="llm", name="fast_llm.analyze")
    def analyze(self, collected: list[dict]) -> tuple[str, dict]:
        """여러 주문 사이에 공통 패턴(같은 택배사 동시 지연 등)이 있는지 비교."""
        orders = [c["data"] for c in collected
                  if c["source"] == "order" and c["data"].get("found", True)]
        issues = [o["order_id"] for o in orders if o.get("issue")]
        if len(issues) >= 2:
            summary = f"{', '.join(issues)} 여러 주문에 동시에 문제가 있음 — 공통 원인 확인 필요"
        elif issues:
            summary = f"{issues[0]}만 문제가 있음 — 다른 주문은 정상 진행 중"
        else:
            summary = "조회한 주문 모두 정상 진행 중"
        return summary, self._usage(str(collected), summary)

    @traceable(run_type="llm", name="fast_llm.chat")
    async def chat(self, query: str, history: list[dict], on_token=None) -> tuple[str, dict]:
        """direct 경로 + 세션 기억 증명용 — 히스토리에서 이름을 기억해낸다.

        fake 백엔드는 실제 네트워크 호출이 없어 토큰 단위로 쪼갤 대상이 없다 —
        ollama_llm.py와 인터페이스(on_token 콜백)만 맞추고 결과를 한 번에 반환한다."""
        reply = None
        for msg in reversed(history):
            m = re.search(r"제 이름은 (\S+?)입니다", msg.get("content", ""))
            if m and re.search(r"이름이 뭐", query):
                reply = f"{m.group(1)}님이라고 말씀하셨어요. 무엇을 도와드릴까요?"
                break
        if reply is None:
            m = re.search(r"제 이름은 (\S+?)입니다", query)
            if m:
                reply = f"안녕하세요, {m.group(1)}님! 무엇을 도와드릴까요?"
            else:
                reply = ("안녕하세요! 솔방울마켓 상담 챗봇입니다. "
                         "주문·배송·반품 문의를 도와드릴게요. 무엇을 도와드릴까요?")
        if on_token is not None:
            on_token(reply)
        return reply, self._usage(query, reply)


def _policy_lines(text: str, limit: int = 2) -> list[str]:
    """정책 본문에서 번호 항목만 앞에서부터 limit개 뽑는다 (숫자를 바꾸지 않고 그대로)."""
    return [m.group(1) for line in text.splitlines()
            if (m := re.match(r"^\d+\.\s+(.*)", line.strip()))][:limit]


class QualityLLM(FakeLLM):
    """최종 답변 작성 전담 (고품질·고비용)."""

    @traceable(run_type="llm", name="quality_llm.write")
    async def write(self, query: str, collected: list[dict], attempt: int,
                    feedback: str | None, on_token=None) -> tuple[str, dict]:
        """1차 초안은 의도적으로 저품질(너무 짧음) → validator가 반려하면
        수집된 사실만으로 구조화된 2차 초안을 내놓는다 (재작성 루프 재현)."""
        if attempt == 1:
            draft = "확인해 보니 곧 처리될 예정입니다."
            if on_token is not None:
                on_token(draft)
            return draft, self._usage(query, draft)

        orders = [c for c in collected if c["source"] == "order"]
        policies = [c for c in collected if c["source"] == "policy"]
        confirm = []
        for c in orders:
            d = c["data"]
            if not d.get("found", True):
                confirm.append(f"{d['order_id']}는 고객님 주문 내역에서 확인되지 않습니다")
                continue
            items = ", ".join(i["name"] for i in d["items"])
            confirm.append(f"{d['order_id']}({items})는 현재 {d['status_label']} 상태입니다"
                           + (f" — {d['issue']}" if d.get("issue") else ""))
        for c in collected:
            if c["source"] == "product":
                confirm += ([f"{p['name']}의 판매가는 {p['price']:,}원입니다"
                             for p in c["data"]["products"]]
                            or ["말씀하신 상품을 찾지 못했습니다"])
        lines = ["1) 주문·상품 확인: " + ("; ".join(confirm) or "특정 주문 없이 정책으로 안내드립니다") + "."]
        guide = [f"'{p['topic']}' 정책: " + " ".join(_policy_lines(p["data"])) for p in policies]
        lines.append("2) 정책 안내: " + (" / ".join(guide) or "관련 정책을 찾지 못했습니다."))
        lines.append("3) 다음 단계: 신청은 마이페이지 > 주문 내역에서 하실 수 있고, "
                     "추가로 궁금한 점은 고객센터로 문의해 주세요.")
        draft = "\n".join(lines)
        if on_token is not None:
            on_token(draft)
        usage = self._usage(query + str(feedback), draft)
        # 실제 LLM처럼 '작성자가 본 사실'을 남긴다 — 평가 스크립트가 근거 검사에 쓴다
        usage["prompt"] = f"[USER]\n{query}\n\n## 수집된 사실\n{format_facts(collected)}"
        return draft, usage
