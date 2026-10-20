"""data/shop/policies/*.md를 임베딩해 Qdrant 컬렉션에 적재한다 (RAG 인덱싱 단계).

실행 (프로젝트 루트에서):
    QDRANT_PATH=./qdrant_local python scripts/index_policies.py      # Docker 없이 로컬 파일 모드 (임베딩: text-embedding-3-large)
    python scripts/index_policies.py --check "받은 신발이 찢어져 있어요"

- 정책 문서 1개 = Qdrant point 1개 (청킹 단위 = 문서, 이유는 mcp_tools/policies.py 참고)
- payload에 topic·title·keywords를 메타데이터로 저장
- 매번 컬렉션을 지우고 새로 만든다 (정책을 수정했으면 다시 실행하면 됨)
- 로컬 파일 모드는 한 프로세스만 열 수 있다 — MCP 서버를 띄우기 '전에' 실행할 것
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from mcp_tools.policies import PolicySearcher  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", help="인덱싱 후 이 질의로 검색해 결과를 출력")
    args = parser.parse_args()

    searcher = PolicySearcher.from_env()
    if not searcher.uses_vectors:
        sys.exit("POLICY_SEARCH=hybrid 또는 qdrant일 때만 인덱싱합니다.")
    count = searcher.build_index()
    print(f"[index] 정책 문서 {count}개 → Qdrant 컬렉션 '{searcher.collection}' "
          f"(embedder={searcher.embedder.name})")
    for p in searcher.catalog:
        print(f"  - {p.topic:18s} {p.title}")

    if args.check:
        result = searcher.search(args.check, top_k=3)
        print(f"\n[check] '{args.check}' (method={result['method']})")
        for h in result["hits"]:
            print(f"  {h['score']:.4f}  {h['topic']}")


if __name__ == "__main__":
    main()
