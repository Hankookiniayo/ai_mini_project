"""정답 기반 회귀 평가 — 같은 질문을 설정만 다른 서버에 똑같이 던지고 지표로 비교한다.

LLM 심판(LLM-as-a-Judge)을 쓰지 않는다 — 정답이 있는 지표만 쓴다.

지표
  route_acc        라우팅 정답률 (chat / order)
  order_recall     조회해야 할 주문 중 실제로 조회한 비율
  order_precision  조회한 주문 중 정답 주문의 비율 (엉뚱한 주문까지 끌어오면 떨어짐)
  policy_recall    기대 정책 그룹 중 실제로 가져온 비율             ← RAG 비교의 핵심 지표
  policy_recall_para  위 지표를 '바꿔 말한' 질문(paraphrase=true)만으로 계산
  grounded         답변의 금액·기간·주문번호·날짜가 전부 사실에 있는 답변 비율  ← 검증 비교
  value_coverage   답변에 꼭 나와야 할 값(주문번호·기간·금액)이 나온 비율
  memory_ok        대화 기억(이름) 정답 여부
  validation_tries validate 시도 횟수 평균 (재작성 비용)
  safe_fallback    검수에 끝내 실패해 '안전한 기본 답변'으로 바뀐 비율 (안전의 대가 — 답이 덜 구체적)
  latency_s / cost_usd

Before/After 측정 예 (서버를 설정만 바꿔 다시 띄운 뒤 같은 데이터셋으로)
  python eval/run_eval.py --label keyword          # POLICY_SEARCH=keyword 로 띄운 서버
  python eval/run_eval.py --label rag              # POLICY_SEARCH=qdrant 로 띄운 서버
  python eval/run_eval.py --compare eval/results/keyword.json eval/results/rag.json
"""
import argparse
import json
import uuid
from pathlib import Path

from common import (ROOT, ask, fetched_policies, grounded, login, looked_up_orders, mean,
                    order_scores, policy_recall, print_table, value_coverage)

RESULTS = ROOT / "eval" / "results"
METRICS = ["route_acc", "order_recall", "order_precision", "policy_recall",
           "policy_recall_para", "grounded",
           "value_coverage", "memory_ok", "validation_tries", "safe_fallback",
           "latency_s", "cost_usd"]


def run_case(base: str, case: dict, label: str, cookies) -> dict:
    thread = f"eval-{label}-{case['id']}-{uuid.uuid4().hex[:6]}"
    turns = case.get("turns") or [case["query"]]
    total_latency, total_cost = 0.0, 0.0
    for msg in turns:                      # 다중 턴이면 같은 thread로 순서대로 보낸다
        out = ask(base, msg, thread, cookies)
        total_latency += out["latency_s"]
        total_cost += out["metadata"].get("cost_usd") or 0.0
    query = turns[-1]
    events = out["events"]
    route = next((e.get("route") for e in events if e.get("type") == "RouteDecided"), None)
    looked = looked_up_orders(events)
    fetched = fetched_policies(events)
    recall, precision = order_scores(looked, case.get("expected_orders"))
    is_order = case.get("expected_route") == "order"
    return {
        "id": case["id"], "query": query, "reply": out["reply"], "route": route,
        "prompt": out["prompt"],   # 작성자가 본 사실 — 채점 규칙이 바뀌어도 다시 채점할 수 있게 저장
        "paraphrase": bool(case.get("paraphrase")),
        "looked_up_orders": looked, "fetched_policies": fetched,
        "route_acc": (None if "expected_route" not in case
                      else float(route == case["expected_route"])),
        "order_recall": recall,
        "order_precision": precision,
        "policy_recall": policy_recall(fetched, case.get("expected_policies")),
        "policy_recall_para": (policy_recall(fetched, case.get("expected_policies"))
                               if case.get("paraphrase") else None),
        "grounded": grounded(out["reply"], out["prompt"], query) if is_order else None,
        "value_coverage": value_coverage(out["reply"], case.get("expected_values")),
        "memory_ok": (None if "expected_reply_contains" not in case
                      else float(case["expected_reply_contains"] in out["reply"])),
        "validation_tries": (sum(1 for e in events if e.get("type") == "Validation")
                             or None),
        "safe_fallback": (float(any(e.get("type") == "SafeFallback" for e in events))
                          if is_order else None),
        "latency_s": round(total_latency, 2),
        "cost_usd": round(total_cost, 6),
    }


def summarize(results: list[dict]) -> dict:
    return {m: mean([r.get(m) for r in results], 6 if m == "cost_usd" else 3) for m in METRICS}


def compare(path_a: str, path_b: str) -> None:
    a, b = (json.loads(Path(p).read_text()) for p in (path_a, path_b))
    rows = []
    for m in a["summary"]:
        x, y = a["summary"].get(m), b["summary"].get(m)
        delta = None if x is None or y is None else round(y - x, 6 if m == "cost_usd" else 3)
        rows.append((m, x, y, delta))
    print(f"\n{a['label']}  vs  {b['label']}\n")
    print_table(rows, ("metric", a["label"], b["label"], "delta"))
    print("\n[case별 정책 문서 / 근거]")
    ra = {r["id"]: r for r in a["cases"]}
    for r in b["cases"]:
        before = ra.get(r["id"], {})
        if r.get("policy_recall") is None and r.get("grounded") is None:
            continue
        print(f"  {r['id']}  {before.get('fetched_policies')} → {r['fetched_policies']}"
              f"  (policy {before.get('policy_recall')} → {r['policy_recall']}, "
              f"grounded {before.get('grounded')} → {r['grounded']})")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", help="결과 파일 이름 (keyword / rag / basic / rules ...)")
    parser.add_argument("--base", default="http://127.0.0.1:8100")
    parser.add_argument("--dataset", default=str(ROOT / "eval" / "dataset.json"))
    parser.add_argument("--only", help="쉼표로 구분한 case id만 실행 (예: s01,s07)")
    parser.add_argument("--login", default="sol:mock1234",
                        help="아이디:비밀번호 — 데이터셋 정답이 목업회원 1 기준이라 기본값은 sol")
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    args = parser.parse_args()

    if args.compare:
        compare(*args.compare)
        return
    if not args.label:
        parser.error("--label 또는 --compare가 필요합니다")
    cases = json.loads(Path(args.dataset).read_text())["cases"]
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]

    health = __import__("httpx").get(f"{args.base}/health", timeout=10).json()
    print(f"[eval] label={args.label} server={health.get('llm_backend')} "
          f"fast={health.get('fast_llm')} quality={health.get('quality_llm')}")
    cookies = login(args.base, *args.login.split(":", 1))
    results = []
    for case in cases:
        r = run_case(args.base, case, args.label, cookies)
        results.append(r)
        print(f"  {r['id']}  route={r['route']}  orders={r['looked_up_orders']}  "
              f"policies={r['fetched_policies']}  grounded={r['grounded']}  "
              f"values={r['value_coverage']}  {r['latency_s']}s")

    summary = summarize(results)
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{args.label}.json"
    out.write_text(json.dumps({"label": args.label, "server": health, "summary": summary,
                               "cases": results}, ensure_ascii=False, indent=2))
    print()
    print_table([(m, summary[m]) for m in METRICS], ("metric", args.label))
    print(f"\n저장: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
