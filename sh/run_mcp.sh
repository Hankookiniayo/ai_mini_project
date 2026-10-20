#!/usr/bin/env bash
#
# MCP 서버를 '별도 프로세스(HTTP)'로 띄운다.
#   ops  : http://127.0.0.1:8200/mcp  (주문·상품 조회 Tool · 정책 Resource/검색 Tool · 상담 Prompt)
#
# 에이전트(server.py)는 MCP_TRANSPORT=http로 이 주소에 접속만 한다 — 에이전트를 재시작해도
# MCP는 그대로 떠 있고, MCP 쪽 설정(정책 검색 방식 등)을 바꿔도 에이전트는 그대로다.
#
# 정책 검색 방식 자동 감지 (POLICY_SEARCH를 직접 지정하면 그 값을 따름)
#   1) ./qdrant_local 인덱스가 있으면  → hybrid (벡터+키워드, QDRANT_PATH 로컬 파일 모드, Docker 불필요)
#   2) Qdrant 서버(:6333)가 떠 있으면  → hybrid (서버 모드)
#   3) 둘 다 없으면                    → keyword (Before 비교용 기본 검색)
#   인덱스 만들기: bash sh/index_policies.sh
#
# 종료: 이 터미널에서 Ctrl+C
#
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x ".venv/bin/python3" ] && export PATH="$PWD/.venv/bin:$PATH"
if [ -f .env ]; then set -a; source .env; set +a; fi

if [ -z "${POLICY_SEARCH:-}" ]; then
    if [ -d ./qdrant_local ]; then
        POLICY_SEARCH=hybrid; export QDRANT_PATH="${QDRANT_PATH:-./qdrant_local}"
    elif curl -sf http://127.0.0.1:6333/readyz >/dev/null 2>&1; then
        POLICY_SEARCH=hybrid
    else
        POLICY_SEARCH=keyword
    fi
fi
export POLICY_SEARCH

lsof -ti:8200 | xargs kill -9 2>/dev/null || true
lsof -ti:8201 | xargs kill -9 2>/dev/null || true   # 예전 decoy 서버가 남아 있으면 정리

cleanup() { echo; echo "MCP 서버를 종료합니다..."; kill $OPS_PID 2>/dev/null || true; }
trap cleanup EXIT

python3 mcp_server.py --transport http --port 8200 &
OPS_PID=$!

for _ in $(seq 1 30); do
    curl -s -o /dev/null http://127.0.0.1:8200/mcp && break
    sleep 0.5
done

cat <<MSG

################################################################
 MCP 서버가 떠 있습니다 (별도 프로세스, Streamable HTTP)
   ops  : http://127.0.0.1:8200/mcp
   POLICY_SEARCH=$POLICY_SEARCH ${QDRANT_PATH:+(QDRANT_PATH=$QDRANT_PATH)}
################################################################
 다음: (새 터미널) bash sh/run_agent_hybrid.sh   또는   bash sh/run_agent_fake.sh
MSG

wait $OPS_PID
