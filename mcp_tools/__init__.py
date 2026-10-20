"""MCP 서버(mcp_server.py) 내부 구현 모듈.

에이전트(agent/)는 이 패키지를 import하지 않는다 — 에이전트는 MCP 프로토콜로
Tool/Resource를 부를 뿐이고, 데이터가 어디서 오는지(목업 JSON인지 실제 주문 DB인지,
정책 검색이 Qdrant인지 키워드인지)는 전부 이 패키지 안에 갇혀 있다.

- shop.py       : 주문·배송 조회 (data/shop/*.json). 화면용 API(server.py)도 같은 클래스를 쓴다
- policies.py   : 정책 문서 카탈로그 파싱 + 검색 (qdrant | keyword)
- embeddings.py : 임베딩 백엔드 (openai | fake)
"""
