"""validate 노드의 규칙 기반 검사 — LLM 없이 코드로 판정한다 (비용 0, 결정론).

상담 답변에서 가장 위험한 실수는 "그럴듯하지만 틀린 값"이다. 없는 주문번호, 실제와 다른
도착일, 정책에 없는 "14일 이내 반품 가능" 같은 문장은 고객이 그대로 믿고 행동한다.
그래서 LLM이 쓴 답변을 코드가 한 번 더 검증한다 (교재 7장 권위 게이트와 같은 원칙).

1) format    : 30자 이상 + '1)' 번호 형식 (베이스 에이전트의 검사 그대로)
2) numbers   : 단위(원·일·영업일·시간·개·건·%)가 붙은 숫자가 '수집된 사실'(주문 데이터 +
               정책 발췌) 또는 사용자 질문에 있는가 → 금액·기간 환각 탐지
               (정책 기간을 지어내면 정책 발췌에 그 숫자가 없어서 여기서 걸린다)
3) order_ids : 답변에 나온 주문번호가 실제로 조회한 주문인가 → 주문번호 환각 탐지
4) dates     : 답변의 날짜(10월 17일, 2026-10-17, 10/17)가 사실에 있는가 → 날짜 환각 탐지
5) coverage  : 조회한 주문의 주문번호가 답변에 빠짐없이 나오는가 → 누락 탐지

VALIDATION_MODE=basic 이면 1)만 본다 (Before/After 비교용 — 개선 전 동작).
모든 함수는 순수 함수라 tests/test_validators.py에서 바로 단위 테스트한다.
"""
import re

from .facts import format_facts

_NUM = r"\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?"            # 89,000 / 89000 / 2.5
_CITED = re.compile(rf"({_NUM})\s*(원|영업일|일|시간|개|건|%)")
_ANY_NUM = re.compile(_NUM)
_ORDER_ID = re.compile(r"ORD-\d{4}")
_ISO_DATE = re.compile(r"\d{4}-(\d{2})-(\d{2})")
_KO_DATE = re.compile(r"(\d{1,2})월\s*(\d{1,2})일")
_SLASH_DATE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])")


def _num(text: str) -> float:
    return float(text.replace(",", ""))


def cited_numbers(text: str) -> list[float]:
    """단위가 붙은 숫자만 뽑는다 — '1)' 같은 번호나 'ORD-1004'의 1004는 대상이 아니다."""
    return [_num(m.group(1)) for m in _CITED.finditer(text)]


def _matches(a: float, facts: list[float]) -> bool:
    for b in facts:
        if abs(a - b) <= max(0.05, abs(b) * 0.01):   # 동일 값 (소수점 표기 차이 허용)
            return True
    return False


# '7일 이내', '3일째', '3영업일'처럼 기간을 뜻하는 표현 — 날짜(10월 14일의 14)와 구분한다
_DAY_SPAN = re.compile(rf"({_NUM})\s*(영업일|일)(?=\s*(이내|안에|내에|내|후|뒤|째|동안|간|만에|정도|이상|이하|경과|지남|늦|소요|걸))")
_DAY_ANY = re.compile(rf"({_NUM})\s*(영업일|일)")


def check_numbers(draft: str, facts_text: str) -> list[float]:
    """사실에 없는 숫자 목록을 돌려준다 (비어 있으면 통과).

    금액·개수·% 는 사실 텍스트에 그 숫자가 있으면 통과.
    '일' 단위는 더 엄격하게 본다 — [3차 수정] 처음에는 사실 어디에든 숫자가 있으면 통과시켰더니,
    "환불은 14일 이내"가 회수일 '2026-10-14'의 14 때문에 통과했다.
      · 기간 표현(14일 이내, 3영업일)은 사실에 '기간으로' 나온 숫자와만 맞춰 본다
      · 그냥 'N일'(예: 17일 도착)은 기간 숫자 또는 사실 속 날짜의 '일'과 맞춰 본다"""
    facts_all = [_num(x) for x in _ANY_NUM.findall(facts_text)]
    fact_spans = [_num(m.group(1)) for m in _DAY_ANY.finditer(facts_text)]
    fact_days = fact_spans + [float(d) for _, d in _ISO_DATE.findall(facts_text)]
    # '10월 22일'의 22일은 날짜라서 check_dates가 따로 본다 — 여기서 또 잡으면 이중 반려가 된다
    draft = _KO_DATE.sub(" ", draft)
    spans = {m.start() for m in _DAY_SPAN.finditer(draft)}
    unknown = []
    for m in _CITED.finditer(draft):
        a, unit = _num(m.group(1)), m.group(2)
        if unit == "영업일" or m.start() in spans:
            pool = fact_spans
        elif unit == "일":
            pool = fact_days
        else:
            pool = facts_all
        if not _matches(a, pool):
            unknown.append(a)
    return unknown


def check_order_ids(draft: str, facts_text: str) -> list[str]:
    """답변에는 있는데 사실에는 없는 주문번호 (비어 있으면 통과)."""
    known = set(_ORDER_ID.findall(facts_text))
    return sorted({o for o in _ORDER_ID.findall(draft) if o not in known})


def month_days(text: str) -> set[tuple[int, int]]:
    """텍스트 속 날짜를 (월, 일) 쌍으로 모은다 — 표기법이 달라도 같은 날짜로 비교하기 위함."""
    found = {(int(m), int(d)) for m, d in _ISO_DATE.findall(text)}
    found |= {(int(m), int(d)) for m, d in _KO_DATE.findall(text)}
    found |= {(int(m), int(d)) for m, d in _SLASH_DATE.findall(text)
              if 1 <= int(m) <= 12 and 1 <= int(d) <= 31}
    return found


def check_dates(draft: str, facts_text: str) -> list[str]:
    """답변에는 있는데 사실에는 없는 날짜 (비어 있으면 통과)."""
    known = month_days(facts_text)
    return [f"{m}월 {d}일" for m, d in sorted(month_days(draft) - known)]


def check_coverage(draft: str, order_ids: list[str]) -> list[str]:
    """답변에 주문번호가 없는 주문 목록 (비어 있으면 통과)."""
    return [o for o in order_ids if o not in draft]


def check_format(draft: str) -> bool:
    return len(draft) >= 30 and "1)" in draft


def looked_up_orders(collected: list[dict]) -> list[str]:
    """이번 상담에서 실제로 조회에 성공한 주문번호 (조회 실패·없는 번호는 제외)."""
    seen = []
    for c in collected:
        if c["source"] == "order" and c["data"].get("found", True):
            if c["data"]["order_id"] not in seen:
                seen.append(c["data"]["order_id"])
    return seen


def run_checks(draft: str, collected: list[dict], query: str, mode: str = "rules") -> dict:
    fmt_ok = check_format(draft)
    unknown = unknown_ids = unknown_dates = missing = []
    if mode != "basic":
        facts_text = format_facts(collected) + "\n" + query
        unknown = check_numbers(draft, facts_text)
        unknown_ids = check_order_ids(draft, facts_text)
        unknown_dates = check_dates(draft, facts_text)
        missing = check_coverage(draft, looked_up_orders(collected))

    feedback = []
    if not fmt_ok:
        feedback.append(f"응답이 너무 짧거나 번호 형식이 없습니다({len(draft)}자). "
                        f"1) 주문 확인 2) 정책 안내 3) 다음 단계 형식으로 쓰세요. "
                        f"조회한 주문이 없어도 1)을 빼지 말고 '특정 주문 없이 안내'라고 쓰세요.")
    if unknown:
        nums = ", ".join(f"{n:g}" for n in unknown)
        feedback.append(f"수집된 사실에 없는 수치가 있습니다: {nums}. "
                        f"금액·기간은 주문 정보와 정책 발췌의 값만 그대로 인용하세요.")
    if unknown_ids:
        feedback.append(f"조회하지 않은 주문번호가 있습니다: {', '.join(unknown_ids)}. "
                        f"조회한 주문번호만 쓰세요.")
    if unknown_dates:
        feedback.append(f"수집된 사실에 없는 날짜가 있습니다: {', '.join(unknown_dates)}. "
                        f"날짜는 주문·배송 기록에 있는 값만 쓰세요.")
    if missing:
        feedback.append(f"조회한 주문이 답변에 빠졌습니다: {', '.join(missing)}. "
                        f"각 주문번호를 그대로 언급하고 상태를 설명하세요.")
    return {
        "passed": not feedback,
        "checks": {"format": fmt_ok, "numbers": not unknown, "order_ids": not unknown_ids,
                   "dates": not unknown_dates, "coverage": not missing},
        "unknown_numbers": unknown,
        "unknown_orders": unknown_ids,
        "unknown_dates": unknown_dates,
        "missing_orders": missing,
        "feedback": " ".join(feedback) or None,
    }
