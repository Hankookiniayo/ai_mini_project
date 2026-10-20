"""LLM_BACKEND=openai — 실제 OpenAI API 백엔드 (fake_llm/ollama_llm과 동일 인터페이스).

fast(기본 gpt-4o-mini)는 분류·계획·단순 대화, quality(기본 gpt-4o)는 최종 상담 답변 작성 전담
(교재 11장 모델 티어링). 실모델이라 결정론이 아니므로 gate·replan·validator가 연출이 아니라
실제 방어선으로 동작한다. 실제 과금이 발생하는 원격 API다.

구조화 출력은 `response_format={"type": "json_object"}`(JSON 모드)로 강제한다 — 스키마까지
강제하지는 않으므로 prompts.parse_route/parse_plan과 planning.normalize_plan이 코드로 보정한다.

사용자에게 보이는 텍스트(chat, write)는 stream=True로 받아 토큰 단위로 on_token에 넘긴다
— 채팅 화면에 답변이 타이핑되듯 나타난다.

비용(cost_usd)은 응답의 total_tokens(실측) × 가상 단가다 — 실제 청구 금액과는 별개로,
모델 분리 효과를 fake/ollama와 같은 잣대로 비교하기 위함이다.

사전 조건: 유효한 OPENAI_API_KEY 환경변수(.env).
"""
import json
import os

import httpx
from langsmith import traceable

from . import prompts
from .facts import format_facts

OPENAI_URL = "https://api.openai.com/v1/chat/completions"


class OpenAILLM:
    def __init__(self, model: str, usd_per_1k_tokens: float):
        self.name = f"openai:{model}"
        self.model = model
        self.usd_per_1k = usd_per_1k_tokens
        self.api_key = os.environ.get("OPENAI_API_KEY", "")

    def _headers(self) -> dict:
        if not self.api_key:
            raise RuntimeError(
                "OPENAI_API_KEY가 설정되지 않았습니다 — .env 또는 환경변수로 지정하세요.")
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _usage(self, tokens: int) -> dict:
        return {"model": self.name, "tokens": tokens,
                "usd": round(tokens / 1000 * self.usd_per_1k, 6)}

    def _chat(self, system: str, user: str, json_mode: bool = False) -> tuple[str, dict]:
        """한 번에 받는 호출 — JSON 출력(route/plan)처럼 부분 출력이 의미 없는 단계용."""
        payload = {"model": self.model, "temperature": 0,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}]}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        resp = httpx.post(OPENAI_URL, json=payload, headers=self._headers(), timeout=120.0)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return content, self._usage(data.get("usage", {}).get("total_tokens", 0))

    async def _chat_stream(self, system: str, user: str, on_token=None) -> tuple[str, dict]:
        """사용자 대면 텍스트용 — SSE 스트림을 받아 조각마다 on_token을 호출한다.
        stream_options.include_usage=True면 마지막 청크에 토큰 사용량이 실려 온다."""
        payload = {"model": self.model, "temperature": 0, "stream": True,
                   "stream_options": {"include_usage": True},
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}]}
        chunks: list[str] = []
        tokens = 0
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", OPENAI_URL, json=payload,
                                     headers=self._headers()) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    event = json.loads(data)
                    if event.get("usage"):
                        tokens = event["usage"].get("total_tokens", 0)
                    for choice in event.get("choices", []):
                        piece = choice.get("delta", {}).get("content")
                        if piece:
                            chunks.append(piece)
                            if on_token is not None:
                                on_token(piece)
        usage = self._usage(tokens)
        usage["prompt"] = f"[SYSTEM]\n{system}\n\n[USER]\n{user}"
        return "".join(chunks), usage


class OpenAIFastLLM(OpenAILLM):
    """분류·계획·단순 대화 전담 (저비용 모델, 기본 gpt-4o-mini)."""

    @traceable(run_type="llm", name="openai_fast.route")
    def route(self, query: str) -> tuple[dict, dict]:
        content, usage = self._chat(prompts.ROUTE_SYSTEM + " " + prompts.ROUTE_JSON_HINT,
                                    query, json_mode=True)
        return prompts.parse_route(content, query), usage

    @traceable(run_type="llm", name="openai_fast.plan")
    def plan(self, query: str, procedure_prompt: str, orders: list[dict]) -> tuple[dict, dict]:
        content, usage = self._chat(prompts.PLAN_SYSTEM + " " + prompts.PLAN_JSON_HINT,
                                    prompts.plan_user_message(query, procedure_prompt, orders),
                                    json_mode=True)
        return prompts.parse_plan(content), usage

    @traceable(run_type="llm", name="openai_fast.analyze")
    def analyze(self, collected: list[dict]) -> tuple[str, dict]:
        return self._chat(prompts.ANALYZE_SYSTEM, prompts.analyze_facts(collected))

    @traceable(run_type="llm", name="openai_fast.chat")
    async def chat(self, query: str, history: list[dict], on_token=None) -> tuple[str, dict]:
        rendered = "\n".join(f"{m['role']}: {m['content']}" for m in history[-10:])
        user = (f"## 지금까지의 대화\n{rendered or '(없음)'}\n\n"
                f"## 사용자의 새 메시지\n{query}")
        return await self._chat_stream(prompts.CHAT_SYSTEM, user, on_token=on_token)


class OpenAIQualityLLM(OpenAILLM):
    """최종 상담 답변 작성 전담 (고품질 모델, 기본 gpt-4o)."""

    @traceable(run_type="llm", name="openai_quality.write")
    async def write(self, query: str, collected: list[dict], attempt: int,
                    feedback: str | None, on_token=None) -> tuple[str, dict]:
        # 사실 포맷팅은 agent/facts.py 한 곳에서 — validate가 같은 텍스트로 검사한다
        user = prompts.write_user_message(query, format_facts(collected), feedback)
        return await self._chat_stream(prompts.WRITE_SYSTEM, user, on_token=on_token)
