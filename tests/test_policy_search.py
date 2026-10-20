"""정책 문서 카탈로그·검색 단위 테스트 (Qdrant는 in-memory 모드, 임베딩은 fake)."""
import pytest
from qdrant_client import QdrantClient

from mcp_tools.embeddings import HashEmbedder
from mcp_tools.policies import PolicySearcher, load_catalog, split_frontmatter


def test_catalog_has_metadata():
    catalog = {p.topic: p for p in load_catalog()}
    assert len(catalog) == 8
    refund = catalog["refund"]
    assert "환불" in refund.keywords and refund.title == "환불 처리 기간 안내"
    assert refund.body.startswith("# ") and not refund.body.startswith("---")   # 본문엔 머리말 없음
    assert refund.uri == "policy://shop/refund"


def test_split_frontmatter_without_header():
    meta, body = split_frontmatter("# 제목만 있는 문서\n1. 단계")
    assert meta == {} and body.startswith("# 제목")


@pytest.mark.parametrize("query, expected", [
    ("택배가 3일째 안 와요", "delivery-delay"),
    ("환불 언제 돼요?", "refund"),
    ("배송지 주소를 바꾸고 싶어요", "address-change"),
    ("주문한 거 취소하고 싶어요", "order-cancel"),
    ("바지 사이즈 교환하고 싶어요", "size-exchange"),
])
def test_keyword_search_top1(query, expected):
    result = PolicySearcher(load_catalog(), mode="keyword").search(query)
    assert result["method"] == "keyword"
    assert result["hits"][0]["topic"] == expected


def test_keyword_search_misses_paraphrase():
    """키워드 검색의 한계를 기록해 둔다 — '찢어져 있어요'는 어떤 문서와도 단어가 안 겹친다.
    (실제 임베딩 RAG와 비교할 Before/After 사례)"""
    result = PolicySearcher(load_catalog(), mode="keyword").search("받은 신발이 찢어져 있어요")
    assert result["hits"][0]["score"] == 0.0


def test_qdrant_index_and_search_in_memory():
    searcher = PolicySearcher(load_catalog(), mode="qdrant",
                              qdrant_client=QdrantClient(location=":memory:"),
                              embedder=HashEmbedder())
    assert searcher.build_index() == len(searcher.catalog)
    result = searcher.search("환불 처리 기간과 카드 취소 반영", top_k=3)
    assert result["method"] == "qdrant"
    assert result["hits"][0]["topic"] == "refund"
    assert result["hits"][0]["uri"] == "policy://shop/refund"


def test_qdrant_failure_falls_back_to_keyword():
    class BrokenClient:
        def query_points(self, **_):
            raise ConnectionError("qdrant down")

    searcher = PolicySearcher(load_catalog(), mode="qdrant",
                              qdrant_client=BrokenClient(), embedder=HashEmbedder())
    result = searcher.search("환불 언제 돼요?")
    assert result["method"].startswith("keyword (qdrant 실패로 폴백")
    assert result["hits"][0]["topic"] == "refund"


def test_hybrid_mixes_vector_and_keyword_scores():
    """4차 개선: 하이브리드 점수 = w × 벡터 + (1-w) × 키워드."""
    searcher = PolicySearcher(load_catalog(), mode="hybrid",
                              qdrant_client=QdrantClient(location=":memory:"),
                              embedder=HashEmbedder(), hybrid_weight=0.5)
    searcher.build_index()
    result = searcher.search("환불 언제 돼요?", top_k=3)
    assert result["method"] == "hybrid"
    top = result["hits"][0]
    assert top["topic"] == "refund"
    assert abs(top["score"] - (0.5 * top["vector"] + 0.5 * top["keyword"])) < 1e-3


def test_hybrid_falls_back_to_keyword_when_qdrant_down():
    class BrokenClient:
        def query_points(self, **_):
            raise ConnectionError("qdrant down")

    searcher = PolicySearcher(load_catalog(), mode="hybrid",
                              qdrant_client=BrokenClient(), embedder=HashEmbedder())
    result = searcher.search("환불 언제 돼요?")
    assert result["method"].startswith("keyword (qdrant 실패로 폴백")
