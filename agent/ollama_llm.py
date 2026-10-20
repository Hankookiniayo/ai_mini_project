"""LLM_BACKEND=ollama — 로컬 Ollama 실모델 백엔드 (fake_llm과 동일 인터페이스).

fast(기본 qwen3:0.6b)는 분류·계획·단순 대화, quality(기본 qwen3:8b)는 최종 답변 작성 전담.
Ollama의 구조화 출력(format=<JSON Schema>)과 temperature=0으로 변동을 줄이지만 실모델이므로
결정론은 아니다 — 그래서 gate·replan·validator가 실제 방어선으로 동작한다.

비용(cost_usd)은 '가상 단가 × 실측 토큰 수(prompt_eval_count+eval_count)'다 — 로컬
추론이라 실제 과금은 0이지만, 모델 분리 효과를 같은 잣대로 비교하기 위함이다.

프롬프트와 응답 파싱은 agent/prompts.py를 openai_llm.py와 함께 쓴다.

qwen3는 기본으로 '생각(thinking)' 토큰을 먼저 생성한다. 상담 챗봇의 짧은 대화에는 필요 없고
지연만 늘어서(실측: 같은 인사 응답 5.8초 → 1.5초) think=False로 끈다.

사전 조건: `ollama serve` 실행 중 + `ollama pull qwen3:0.6b qwen3:8b`.
"""
import json

import httpx
from langsmith import traceable

from . import prompts
from .facts import format_facts

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"

ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "route": {"type": "string", "enum": ["chat", "order"]},
        "needs_analysis": {"type": "boolean"},
    },
    "required": ["route", "needs_analysis"],
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "needs_order": {"type": "boolean"},
        "needs_product": {"type": "boolean"},
        "needs_analysis": {"type": "boolean"},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "enum": ["research", "analysis"]},
                    "target": {"type": "string"},
                },
                "required": ["agent"],
            },
        },
    },
    "required": ["needs_order", "needs_analysis", "tasks"],
}


class OllamaLLM:
    def __init__(self, model: str, usd_per_1k_tokens: float):
        self.name = f"ollama:{model}"
        self.model = model
        self.usd_per_1k = usd_per_1k_tokens

    def _chat(self, system: str, user: str, schema: dict | None = None) -> tuple[str, dict]:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False,
            "think": False,
            "options": {"temperature": 0.0},
        }
        if schema is not None:
            payload["format"] = schema
        resp = httpx.post(OLLAMA_URL, json=payload, timeout=300.0)
        resp.raise_for_status()
        data = resp.json()
        content = data["message"]["content"]
        tokens = (data.get("prompt_eval_count") or 0) + (data.get("eval_count") or 0)
        usage = {"model": self.name, "tokens": tokens,
                 "usd": round(tokens / 1000 * self.usd_per_1k, 6)}
        return content, usage

    async def _chat_stream(self, system: str, user: str, on_token=None) -> tuple[str, dict]:
        """사용자 대면 텍스트(최종 답변류) 전용 — Ollama NDJSON 스트림을 토큰 단위로
        받아 on_token(조각)을 즉시 호출해 SSE로 실시간 중계할 수 있게 한다. 분류/계획
        등 JSON 스키마 출력(route/plan/analyze)은 부분 출력이 의미가 없어 그대로 _chat을
        쓴다."""
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": True,
            "think": False,
            "options": {"temperature": 0.0},
        }
        chunks: list[str] = []
        usage = {"model": self.name, "tokens": 0, "usd": 0.0}
        async with httpx.AsyncClient(timeout=300.0) as client:
            async with client.stream("POST", OLLAMA_URL, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    piece = data.get("message", {}).get("content", "")
                    if piece:
                        chunks.append(piece)
                        if on_token is not None:
                            on_token(piece)
                    if data.get("done"):
                        tokens = ((data.get("prompt_eval_count") or 0)
                                  + (data.get("eval_count") or 0))
                        usage = {"model": self.name, "tokens": tokens,
                                 "usd": round(tokens / 1000 * self.usd_per_1k, 6)}
        usage["prompt"] = f"[SYSTEM]\n{system}\n\n[USER]\n{user}"
        return "".join(chunks), usage


class OllamaFastLLM(OllamaLLM):
    """분류·계획·단순 대화 전담 (저사양 소형 모델)."""

    @traceable(run_type="llm", name="ollama_fast.route")
    def route(self, query: str) -> tuple[dict, dict]:
        content, usage = self._chat(prompts.ROUTE_SYSTEM, query, schema=ROUTE_SCHEMA)
        return prompts.parse_route(content, query), usage

    @traceable(run_type="llm", name="ollama_fast.plan")
    def plan(self, query: str, procedure_prompt: str, orders: list[dict]) -> tuple[dict, dict]:
        content, usage = self._chat(prompts.PLAN_SYSTEM,
                                    prompts.plan_user_message(query, procedure_prompt, orders),
                                    schema=PLAN_SCHEMA)
        return prompts.parse_plan(content), usage

    @traceable(run_type="llm", name="ollama_fast.analyze")
    def analyze(self, collected: list[dict]) -> tuple[str, dict]:
        return self._chat(prompts.ANALYZE_SYSTEM, prompts.analyze_facts(collected))

    @traceable(run_type="llm", name="ollama_fast.chat")
    async def chat(self, query: str, history: list[dict], on_token=None) -> tuple[str, dict]:
        rendered = "\n".join(f"{m['role']}: {m['content']}" for m in history[-10:])
        user = (f"## 지금까지의 대화\n{rendered or '(없음)'}\n\n"
                f"## 사용자의 새 메시지\n{query}")
        return await self._chat_stream(prompts.CHAT_SYSTEM, user, on_token=on_token)


class OllamaQualityLLM(OllamaLLM):
    """최종 답변 작성 전담 (상대적으로 큰 모델)."""

    @traceable(run_type="llm", name="ollama_quality.write")
    async def write(self, query: str, collected: list[dict], attempt: int,
                    feedback: str | None, on_token=None) -> tuple[str, dict]:
        # 사실 포맷팅은 agent/facts.py 한 곳에서 — validate가 같은 텍스트로 검사한다
        user = prompts.write_user_message(query, format_facts(collected), feedback)
        return await self._chat_stream(prompts.WRITE_SYSTEM, user, on_token=on_token)
