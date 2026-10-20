# 쇼핑몰 주문·배송 상담 에이전트

가상 쇼핑몰 '솔방울마켓'의 주문·배송 상담 챗봇입니다. 고객 질문에 **실제 주문 데이터와 쇼핑몰 정책 문서에 근거해서만** 답하고, 근거가 없으면 고객센터로 안내합니다. 조회·안내만 하며 반품·취소 접수는 하지 않습니다.
(SKALA AI Mini Project · 개인 프로젝트 · 상품과 주문은 모두 목업 데이터)

- 질문 속 주문 자동 식별 및 MCP 기반 주문·상품 조회 서비스 분리
- 키워드+벡터 하이브리드 RAG로 반품·환불·배송 정책 검색
- 답변 속 금액·날짜·주문번호 검증, 근거 없으면 고객센터 안내로 전환
- 정답 기반 Before/After 실험과 LangSmith 추적

## 실행

```bash
bash sh/setup_venv.sh                # 의존성 설치 (.venv)
.venv/bin/python -m pytest -q        # 단위 테스트 (오프라인)

# 과금 없이 화면 보기 (fake LLM)
bash sh/run_agent_fake.sh            # 에이전트 :8100
bash sh/run_frontend.sh              # (새 터미널) http://localhost:8600

# 실제 구성 (OpenAI + Ollama, 과금 있음) — 먼저 cp .env.example .env 후 키 입력
bash sh/index_policies.sh            # 정책 문서 임베딩 → ./qdrant_local
bash sh/run_mcp.sh                   # (새 터미널) MCP 서버 :8200
bash sh/run_agent_hybrid.sh          # (새 터미널) 에이전트 :8100
bash sh/run_frontend.sh              # (새 터미널) http://localhost:8600
```

구조, 구현 내용, Before/After 측정 결과, 한계는 [doc/README.md](doc/README.md)에 정리했습니다.
