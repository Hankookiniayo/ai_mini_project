"""정책 검색 방식 비교 — dev 질문(eval/retrieval_dev.json)으로 방식과 임계치를 고른다.

비교 대상
  vector-small   text-embedding-3-small 코사인 유사도 (3차까지 쓰던 방식)
  vector-large   text-embedding-3-large 코사인 유사도
  hybrid-small   w × 벡터(small) + (1-w) × 키워드 점수
  hybrid-large   w × 벡터(large) + (1-w) × 키워드 점수

채점은 에이전트(graph.py)와 같은 규칙으로 한다:
  1등 점수 ≥ 임계치면 1등을 가져오고, 2등은 임계치 이상이면서 1등의 POLICY_SECOND_RATIO(0.9) 이상일 때만.
  정책이 있는 질문 → 정답 문서를 가져오면 정답 / 정책이 없는 질문 → 아무것도 안 가져오면 정답.
방식마다 임계치를 0.05~0.80 범위에서 훑어 dev 정확도가 가장 높은 값을 찾는다.

평가셋(eval/dataset.json)이 아니라 별도 dev 질문으로 고르는 이유: 평가 질문으로 방식·임계치를
고르면 평가 수치가 실제보다 좋게 나온다 (과적합).

실행 (OpenAI 임베딩 호출 — 질문 30개 + 문서 8개 × 2개 모델, 비용 아주 작음):
    python scripts/compare_retrieval.py
"""
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from mcp_tools.embeddings import OpenAIEmbedder  # noqa: E402
from mcp_tools.policies import PolicySearcher, load_catalog  # noqa: E402

SECOND_RATIO = 0.9
THRESHOLDS = [round(0.05 + 0.01 * i, 2) for i in range(76)]   # 0.05 ~ 0.80
HYBRID_WEIGHTS = [0.3, 0.5, 0.7]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def choose(scores: dict[str, float], threshold: float) -> list[str]:
    """graph.py search_policy와 같은 선택 규칙."""
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best = ranked[0][1]
    if best < threshold:
        return []
    return [t for t, s in ranked[:2] if s >= threshold and s >= best * SECOND_RATIO]


def evaluate(all_scores: list[dict[str, float]], queries: list[dict], threshold: float) -> dict:
    pos_hit = neg_ok = 0
    n_pos = sum(1 for q in queries if q["expected"])
    for scores, q in zip(all_scores, queries):
        picked = choose(scores, threshold)
        if q["expected"]:
            pos_hit += q["expected"] in picked
        else:
            neg_ok += not picked
    n_neg = len(queries) - n_pos
    return {"threshold": threshold, "accuracy": round((pos_hit + neg_ok) / len(queries), 3),
            "positive_hit": f"{pos_hit}/{n_pos}", "negative_reject": f"{neg_ok}/{n_neg}"}


def top1(all_scores: list[dict[str, float]], queries: list[dict]) -> str:
    pos = [(s, q) for s, q in zip(all_scores, queries) if q["expected"]]
    hit = sum(1 for s, q in pos if max(s, key=s.get) == q["expected"])
    return f"{hit}/{len(pos)}"


def main() -> None:
    queries = json.loads((ROOT / "eval" / "retrieval_dev.json").read_text())["queries"]
    catalog = load_catalog()
    keyword = PolicySearcher(catalog, mode="keyword")
    kw_scores = []
    for q in queries:
        hits = keyword.keyword_search(q["q"], top_k=len(catalog))["hits"]
        kw_scores.append({h["topic"]: h["score"] for h in hits})

    vec_scores = {}
    for size in ("small", "large"):
        emb = OpenAIEmbedder(f"text-embedding-3-{size}")
        docs = emb.embed([p.embedding_text() for p in catalog])
        qs = emb.embed([q["q"] for q in queries])
        vec_scores[size] = [{p.topic: cosine(qv, dv) for p, dv in zip(catalog, docs)} for qv in qs]

    methods = {"keyword": kw_scores,
               "vector-small": vec_scores["small"], "vector-large": vec_scores["large"]}
    for size in ("small", "large"):
        for w in HYBRID_WEIGHTS:
            methods[f"hybrid-{size} (w={w})"] = [
                {t: w * v[t] + (1 - w) * k[t] for t in v}
                for v, k in zip(vec_scores[size], kw_scores)]

    print(f"dev 질문 {len(queries)}개 (정책 있음 {sum(1 for q in queries if q['expected'])}, "
          f"정책 없음 {sum(1 for q in queries if not q['expected'])})\n")
    print(f"{'method':24s} {'top1':>6s} {'best_acc':>9s} {'threshold':>10s} {'pos_hit':>8s} {'neg_ok':>7s}")
    results = {}
    for name, scores in methods.items():
        evals = [evaluate(scores, queries, t) for t in THRESHOLDS]
        best_acc = max(e["accuracy"] for e in evals)
        plateau = [e for e in evals if e["accuracy"] == best_acc]
        best = plateau[len(plateau) // 2]   # 같은 정확도 구간의 '가운데' 임계치 — 경계값보다 덜 민감
        results[name] = {**best, "top1": top1(scores, queries),
                         "plateau": [plateau[0]["threshold"], plateau[-1]["threshold"]]}
        print(f"{name:24s} {results[name]['top1']:>6s} {best['accuracy']:>9} "
              f"{best['threshold']:>10} {best['positive_hit']:>8s} {best['negative_reject']:>7s}"
              f"   (같은 정확도 구간 {plateau[0]['threshold']}~{plateau[-1]['threshold']})")
    out = ROOT / "eval" / "results" / "retrieval_dev.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\n저장: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
