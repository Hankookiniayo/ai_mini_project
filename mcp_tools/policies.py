"""쇼핑몰 정책 문서 카탈로그 + 검색 (RAG의 'R').

장애 대응 버전의 runbook 검색(runbooks.py)을 그대로 옮겨 온 것이다. 바뀐 것은 문서
종류와 메타데이터 이름뿐이고 구조(카탈로그 → Qdrant 벡터 검색 → 실패 시 키워드 폴백)는 같다.

정책 문서(data/shop/policies/*.md) 상단의 메타데이터(frontmatter):
      ---
      topic: refund
      title: 환불 처리 기간 안내
      keywords: [환불, 환불 언제, 카드 취소, ...]
      ---
- list_policies   : 카탈로그(어떤 정책 문서가 있는지)를 MCP Tool로 노출
- search_policies : 질의와 가장 가까운 정책 문서를 찾는다. 방식은 POLICY_SEARCH로 고른다.
      hybrid (기본) : w × 벡터 유사도 + (1-w) × 키워드 점수 (text-embedding-3-large, w=0.5)
      qdrant        : 벡터 유사도만
      keyword       : 키워드 점수만
    Qdrant가 없거나 컬렉션이 비었으면 키워드 점수 방식으로 폴백한다
    (Graceful Degradation — 검색 품질은 떨어져도 상담은 계속된다).

[4차 개선] 왜 하이브리드인가 — scripts/compare_retrieval.py, eval/retrieval_dev.json (dev 질문 30개)
    방식              dev 정확도   정답 1위
    keyword            0.667      14/24
    vector-small       0.567      14/24   ← 3차까지 쓰던 방식. 한국어 구어체에 약했다
    vector-large       0.733      20/24
    hybrid-large 0.5   0.800      20/24   ← 채택
  벡터는 '바꿔 말한 표현'에 강하고, 키워드는 '정책 용어를 그대로 쓴 질문'에 강해서 합치면 서로
  약점을 메운다. 평가셋이 아니라 별도 dev 질문으로 골랐다 (평가 수치가 부풀려지지 않게).

청킹 전략: 정책 문서 1개 = 청크 1개.
  문서가 5줄 안팎으로 짧고, 에이전트가 고르는 단위가 '문서(=topic)'이기 때문이다.
  임베딩 텍스트는 제목 + 키워드 + 본문을 합쳐 만든다.
"""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

POLICY_DIR = Path(__file__).resolve().parent.parent / "data" / "shop" / "policies"
COLLECTION = os.environ.get("QDRANT_COLLECTION", "shop_policies")
URI_PREFIX = "policy://shop/"


@dataclass
class Policy:
    topic: str
    title: str
    body: str
    keywords: list[str] = field(default_factory=list)

    @property
    def uri(self) -> str:
        return f"{URI_PREFIX}{self.topic}"

    def embedding_text(self) -> str:
        return f"{self.title}\n관련 표현: {', '.join(self.keywords)}\n{self.body}"

    def meta(self) -> dict:
        return {"topic": self.topic, "title": self.title,
                "keywords": self.keywords, "uri": self.uri}


def _parse_list(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return [v.strip() for v in value.split(",") if v.strip()]


def split_frontmatter(text: str) -> tuple[dict, str]:
    """'---'로 감싼 머리말을 dict로, 나머지를 본문으로 나눈다 (YAML 라이브러리 없이)."""
    meta: dict = {}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return meta, text
    for line in m.group(1).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return meta, m.group(2)


def parse_policy(path: Path) -> Policy:
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    title = meta.get("title") or next(
        (l.lstrip("# ").strip() for l in body.splitlines() if l.startswith("#")), path.stem)
    return Policy(topic=meta.get("topic", path.stem), title=title, body=body.strip(),
                  keywords=_parse_list(meta.get("keywords", "")))


def load_catalog(policy_dir: Path = POLICY_DIR) -> list[Policy]:
    return [parse_policy(p) for p in sorted(policy_dir.glob("*.md"))]


# 어느 문서에나 걸리는 흔한 단어는 키워드 점수에서 뺀다
# (예: '주문'은 거의 모든 질문·문서에 나와서 변별력이 없다).
_STOPWORDS = {"주문", "상품", "제가", "저는", "어떻게", "해주세요", "알려주세요", "궁금해요",
              "문의", "있나요", "되나요", "가능한가요", "그리고", "솔방울마켓"}


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^0-9a-zA-Z가-힣]+", text.lower())
            if len(t) >= 2 and t not in _STOPWORDS]


def _hit(token: str, vocab: set[str]) -> bool:
    """한국어는 조사·어미가 붙어서('반품돼요', '택배가') 완전 일치로는 거의 안 걸린다.
    그래서 한쪽이 다른 쪽으로 시작하면(접두 일치) 같은 단어로 본다 — 형태소 분석기 없이
    쓸 수 있는 가장 단순한 근사다. 의미까지는 못 본다는 게 키워드 검색의 한계."""
    return any(token.startswith(v) or v.startswith(token) for v in vocab)


class PolicySearcher:
    """POLICY_SEARCH=hybrid(기본) | qdrant | keyword."""

    def __init__(self, catalog: list[Policy], mode: str = "hybrid",
                 qdrant_client=None, embedder=None, collection: str = COLLECTION,
                 hybrid_weight: float = 0.5):
        self.catalog = catalog
        self.by_topic = {p.topic: p for p in catalog}
        self.mode = mode
        self.client = qdrant_client
        self.embedder = embedder
        self.collection = collection
        self.hybrid_weight = hybrid_weight   # 벡터 점수의 비중 (나머지는 키워드 점수)

    @property
    def uses_vectors(self) -> bool:
        return self.mode in ("qdrant", "hybrid")

    @classmethod
    def from_env(cls) -> "PolicySearcher":
        mode = os.environ.get("POLICY_SEARCH", "hybrid")
        client = embedder = None
        if mode in ("qdrant", "hybrid"):
            from qdrant_client import QdrantClient

            from .embeddings import get_embedder
            if os.environ.get("QDRANT_PATH"):   # Docker 없이 쓰는 로컬 파일 모드 (단일 프로세스 전용)
                client = QdrantClient(path=os.environ["QDRANT_PATH"])
            else:                                 # Qdrant 서버(docker 등)에 접속
                url = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
                try:   # 클라이언트·서버 버전 차이 경고를 끈다 (기능은 동일하게 동작)
                    client = QdrantClient(url=url, timeout=5, check_compatibility=False)
                except TypeError:   # 구버전 qdrant-client
                    client = QdrantClient(url=url, timeout=5)
            embedder = get_embedder()
        return cls(load_catalog(), mode=mode, qdrant_client=client, embedder=embedder,
                   hybrid_weight=float(os.environ.get("POLICY_HYBRID_WEIGHT", "0.5")))

    # ── 검색 ──────────────────────────────────────────────────────────
    def search(self, query: str, top_k: int = 3) -> dict:
        if self.uses_vectors and self.client is not None:
            try:
                if self.mode == "hybrid":
                    return {"query": query, "method": "hybrid",
                            "hits": self._hybrid_search(query, top_k)}
                return {"query": query, "method": "qdrant",
                        "hits": self._vector_search(query, top_k)}
            except Exception as exc:   # Qdrant 다운·컬렉션 없음·차원 불일치 등
                result = self.keyword_search(query, top_k)
                result["method"] = f"keyword (qdrant 실패로 폴백: {type(exc).__name__})"
                return result
        return self.keyword_search(query, top_k)

    def _vector_search(self, query: str, top_k: int) -> list[dict]:
        vector = self.embedder.embed([query])[0]
        points = self.client.query_points(
            collection_name=self.collection, query=vector, limit=top_k,
            with_payload=True).points
        if not points:
            raise LookupError("빈 컬렉션 — scripts/index_policies.py를 먼저 실행하세요")
        hits = []
        for p in points:
            payload = p.payload or {}
            hits.append({"topic": payload.get("topic"), "title": payload.get("title"),
                         "score": round(float(p.score), 4),
                         "uri": f"{URI_PREFIX}{payload.get('topic')}"})
        return hits

    def _hybrid_search(self, query: str, top_k: int) -> list[dict]:
        """모든 문서의 벡터 점수와 키워드 점수를 w : (1-w)로 섞는다.
        문서가 8개뿐이라 전부 점수를 매겨도 비용이 거의 없다 (문서가 많아지면 각 방식의
        상위 N개만 섞는 식으로 바꿔야 한다)."""
        n = len(self.catalog)
        vector = {h["topic"]: h["score"] for h in self._vector_search(query, n)}
        keyword = {h["topic"]: h["score"] for h in self.keyword_search(query, n)["hits"]}
        w = self.hybrid_weight
        hits = [{"topic": p.topic, "title": p.title, "uri": p.uri,
                 "score": round(w * vector.get(p.topic, 0.0) + (1 - w) * keyword.get(p.topic, 0.0), 4),
                 "vector": vector.get(p.topic), "keyword": keyword.get(p.topic)}
                for p in self.catalog]
        hits.sort(key=lambda h: h["score"], reverse=True)
        return hits[:top_k]

    def keyword_search(self, query: str, top_k: int = 3) -> dict:
        """폴백용 키워드 점수: 질의 토큰 중 문서(키워드·제목·본문)에 등장하는 비율.
        키워드/제목에 걸리면 가중치 2, 본문에만 걸리면 1. 0~1로 정규화."""
        q = _tokens(query)   # 중복을 살린다 — 같은 단어가 여러 번 나오면 그만큼 중요하다
        hits = []
        for p in self.catalog:
            strong = set(_tokens(" ".join(p.keywords + [p.title, p.topic])))
            weak = set(_tokens(p.body))
            raw = sum(2 if _hit(t, strong) else 1 if _hit(t, weak) else 0 for t in q)
            score = raw / (2 * len(q)) if q else 0.0
            hits.append({"topic": p.topic, "title": p.title,
                         "score": round(score, 4), "uri": p.uri})
        hits.sort(key=lambda h: h["score"], reverse=True)
        return {"query": query, "method": "keyword", "hits": hits[:top_k]}

    # ── 인덱싱 (scripts/index_policies.py에서 사용) ─────────────────────
    def build_index(self) -> int:
        from qdrant_client.models import Distance, PointStruct, VectorParams

        vectors = self.embedder.embed([p.embedding_text() for p in self.catalog])
        if self.client.collection_exists(self.collection):
            self.client.delete_collection(self.collection)
        self.client.create_collection(
            self.collection,
            vectors_config=VectorParams(size=len(vectors[0]), distance=Distance.COSINE))
        self.client.upsert(self.collection, points=[
            PointStruct(id=i, vector=v, payload={**p.meta(), "embedder": self.embedder.name})
            for i, (p, v) in enumerate(zip(self.catalog, vectors))
        ])
        return len(self.catalog)
