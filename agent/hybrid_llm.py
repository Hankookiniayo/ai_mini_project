"""LLM_BACKEND=hybrid — 작업 성격별로 서로 다른 '백엔드'를 꽂는다.

기존 _build_llms()는 LLM_BACKEND 하나로 fast/quality 둘 다 같은 백엔드(전부 Ollama 또는
전부 OpenAI)를 썼다. 교재 11장(모델 티어링)을 한 단계 더 밀어서, 백엔드 자체를 작업별로
나눈다.

    route / plan / analyze  → OpenAI fast (gpt-4o-mini)   : 판단이 틀리면 파이프라인 전체가 틀어짐
    chat (가벼운 대화)        → Ollama (로컬, 과금 0)        : 틀려도 피해가 작고 빈도가 높음
    write (최종 상담 답변)    → OpenAI quality (gpt-4o)      : 서버 쪽에서 별도로 주입

route는 "이게 가벼운 대화인지"를 판단하는 단계라서, 대화로 분류되기 '전에' 먼저 돌아야
한다 — 그래서 route는 OpenAI 쪽에 둔다.

이 클래스는 graph.py가 기대하는 fast_llm 인터페이스(route/plan/analyze/chat)를 그대로
구현하므로 graph.py는 한 줄도 바뀌지 않는다 (교재 14장 의존성 주입의 효과).
"""
import httpx
from langsmith import traceable


class HybridFastLLM:
    def __init__(self, planner, chatter):
        self.planner = planner     # OpenAIFastLLM — route/plan/analyze 담당
        self.chatter = chatter     # OllamaFastLLM — chat 담당
        self.name = f"hybrid(plan={planner.name}, chat={chatter.name})"

    def route(self, query: str):
        return self.planner.route(query)

    def plan(self, query: str, procedure_prompt: str, orders: list[dict]):
        return self.planner.plan(query, procedure_prompt, orders)

    def analyze(self, collected: list[dict]):
        return self.planner.analyze(collected)

    @traceable(name="hybrid.chat")
    async def chat(self, query: str, history: list[dict], on_token=None):
        """기본은 Ollama. Ollama가 꺼져 있으면(연결 실패) OpenAI로 폴백한다 —
        로컬 모델 장애가 대화 기능 전체 장애로 번지지 않게 하는 Graceful Degradation."""
        try:
            reply, usage = await self.chatter.chat(query, history, on_token=on_token)
            usage["served_by"] = self.chatter.name
            return reply, usage
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            reply, usage = await self.planner.chat(query, history, on_token=on_token)
            usage["served_by"] = f"{self.planner.name} (fallback: {type(exc).__name__})"
            return reply, usage
