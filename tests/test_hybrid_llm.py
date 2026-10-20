"""하이브리드 LLM 라우팅 단위 테스트 (실제 API 호출 없음)."""
import asyncio

import httpx

from agent.hybrid_llm import HybridFastLLM


class Stub:
    def __init__(self, name, fail=False):
        self.name, self.fail, self.calls = name, fail, []

    def route(self, q):
        self.calls.append("route")
        return {"route": "chat", "needs_analysis": False}, {"usd": 0.0}

    def plan(self, q, p, orders):
        self.calls.append("plan")
        return {"needs_analysis": False, "tasks": []}, {"usd": 0.0}

    def analyze(self, c):
        self.calls.append("analyze")
        return "특이 패턴 없음", {"usd": 0.0}

    async def chat(self, q, h, on_token=None):
        self.calls.append("chat")
        if self.fail:
            raise httpx.ConnectError("ollama is down")
        return f"{self.name} 답변", {"usd": 0.0}


def test_route_plan_analyze_go_to_planner_and_chat_to_chatter():
    planner, chatter = Stub("openai"), Stub("ollama")
    llm = HybridFastLLM(planner, chatter)
    llm.route("안녕")
    llm.plan("x", "y", [])
    llm.analyze([])
    reply, usage = asyncio.run(llm.chat("안녕", []))
    assert planner.calls == ["route", "plan", "analyze"]
    assert chatter.calls == ["chat"]
    assert reply == "ollama 답변" and usage["served_by"] == "ollama"


def test_chat_falls_back_to_planner_when_ollama_down():
    planner, chatter = Stub("openai"), Stub("ollama", fail=True)
    reply, usage = asyncio.run(HybridFastLLM(planner, chatter).chat("안녕", []))
    assert reply == "openai 답변"
    assert "fallback" in usage["served_by"]
