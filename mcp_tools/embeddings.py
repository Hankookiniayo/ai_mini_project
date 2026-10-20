"""임베딩 백엔드 — EMBEDDING_BACKEND=openai(기본) | fake.

- openai : text-embedding-3-large (3072차원, 기본). 실제 의미 기반 검색.
           [4차 개선] 3차까지는 -small을 썼는데 한국어 구어체 질문에서 정답 정책이 3~5위로
           밀렸다. dev 질문 기준 정답 1위가 14/24 → 20/24 (scripts/compare_retrieval.py).
- fake   : 글자 2-gram 해싱 벡터(512차원). API 키·네트워크 없이 단위 테스트와 오프라인
           실행을 하기 위한 대역(test double)이다. 의미가 아니라 '글자 겹침'을 보므로
           품질 비교용으로 쓰면 안 된다.

인덱싱(scripts/index_policies.py)과 검색(policies.py)이 반드시 같은 임베더를 써야 한다
— 차원이 다르면 Qdrant가 오류를 내고, 검색은 키워드 방식으로 폴백한다.
"""
import hashlib
import math
import os
import re

import httpx

OPENAI_EMBED_URL = "https://api.openai.com/v1/embeddings"


class OpenAIEmbedder:
    def __init__(self, model: str = "text-embedding-3-large"):
        self.model = model
        self.name = f"openai:{model}"
        self.api_key = os.environ.get("OPENAI_API_KEY", "")

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY가 없어 임베딩을 만들 수 없습니다.")
        resp = httpx.post(
            OPENAI_EMBED_URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": texts},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]


class HashEmbedder:
    """글자 2-gram을 해싱해 고정 길이 벡터로 만든다 (결정론적, 오프라인)."""

    def __init__(self, dim: int = 512):
        self.dim = dim
        self.name = f"fake:hash{dim}"

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        norm_text = re.sub(r"\s+", " ", text.lower())
        for i in range(len(norm_text) - 1):
            gram = norm_text[i:i + 2]
            if gram.strip() == "":
                continue
            h = int(hashlib.md5(gram.encode()).hexdigest(), 16)
            vec[h % self.dim] += 1.0
        length = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / length for v in vec]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]


def get_embedder():
    backend = os.environ.get("EMBEDDING_BACKEND", "openai")
    if backend == "fake":
        return HashEmbedder()
    return OpenAIEmbedder(os.environ.get("EMBEDDING_MODEL", "text-embedding-3-large"))
