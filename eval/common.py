"""평가 스크립트 공용 함수 — 에이전트를 HTTP(/chat/stream)로 호출하고 채점한다.

에이전트 코드를 import하지 않고 HTTP로만 부르기 때문에, 설정(정책 검색 방식, 검증 모드,
LLM 백엔드)만 바꾼 서버들을 같은 스크립트로 측정할 수 있다 (Before/After 비교의 전제 조건).
채점에 쓰는 순수 함수(검증 규칙)만 agent/validators.py에서 가져온다.
"""
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from agent.validators import check_dates, check_numbers, check_order_ids  # noqa: E402


def ask(base: str, message: str, thread_id: str, timeout: float = 300.0) -> dict:
    """/chat/stream을 끝까지 읽어 이벤트·최종 답변·프롬프트·metadata·지연시간을 모은다."""
    events, reply, prompt, metadata = [], "", None, {}
    started = time.perf_counter()
    with httpx.stream("GET", f"{base}/chat/stream",
                      params={"message": message, "thread_id": thread_id},
                      timeout=timeout) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            ev = json.loads(data)
            if ev.get("type") == "AnswerToken":
                continue
            if ev.get("type") == "FinalAnswer":
                reply = ev.get("reply", "")
                prompt = ev.get("prompt")
                metadata = ev.get("metadata") or {}
                continue
            events.append(ev)
    return {"events": events, "reply": reply, "prompt": prompt, "metadata": metadata,
            "latency_s": round(time.perf_counter() - started, 2)}


def looked_up_orders(events: list[dict]) -> list[str]:
    return sorted({e["target"] for e in events
                   if e.get("type") == "TaskCompleted" and e.get("source") == "order"})


def fetched_policies(events: list[dict]) -> list[str]:
    return sorted({e["target"] for e in events
                   if e.get("type") == "TaskCompleted" and e.get("source") == "policy"})


def order_scores(looked: list[str], expected: list[str] | None) -> tuple:
    """(recall, precision). 기대 주문이 0개인 일반 질문은 '아무것도 조회 안 했나'로 본다."""
    if expected is None:
        return None, None
    if not expected:
        return None, (1.0 if not looked else 0.0)
    hit = len(set(looked) & set(expected))
    recall = round(hit / len(expected), 3)
    precision = round(hit / len(looked), 3) if looked else 0.0
    return recall, precision


def policy_recall(fetched: list[str], groups: list[list[str]] | None) -> float | None:
    if not groups:
        return None
    return round(sum(1 for g in groups if set(g) & set(fetched)) / len(groups), 3)


def grounded(reply: str, prompt: str | None, query: str) -> float | None:
    """답변의 금액·기간·주문번호·날짜가 '작성자가 받은 사실(프롬프트)'에 모두 있으면 1.0.
    하나라도 지어낸 값이 있으면 0.0 — 환각 없는 답변의 비율을 잰다."""
    if not prompt:
        return None
    facts = prompt + "\n" + query
    bad = check_numbers(reply, facts) or check_order_ids(reply, facts) or check_dates(reply, facts)
    return 0.0 if bad else 1.0


def value_coverage(reply: str, groups: list[list[str]] | None) -> float | None:
    """기대 값 그룹 중 답변에 나온 비율. 금액 쉼표(6,000원 ↔ 6000원)는 무시한다."""
    if not groups:
        return None
    text = reply.replace(",", "")
    return round(sum(1 for g in groups if any(v.replace(",", "") in text for v in g))
                 / len(groups), 3)


def mean(values: list, digits: int = 3) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), digits) if vals else None


def print_table(rows: list[tuple], headers: tuple) -> None:
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * w for w in widths]))
    for r in rows:
        print(fmt.format(*["-" if x is None else x for x in r]))
