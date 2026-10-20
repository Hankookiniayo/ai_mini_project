#!/usr/bin/env bash
#
# 정책 문서(data/shop/policies/*.md)를 임베딩해 Qdrant에 적재한다 (RAG 인덱싱).
#
# 기본은 Docker 없이 쓰는 로컬 파일 모드(./qdrant_local). 임베딩은 OpenAI
# text-embedding-3-large를 쓰므로 .env에 OPENAI_API_KEY가 필요하다 (문서 8개, 비용 아주 작음).
#
# ⚠️ 로컬 파일 모드는 한 프로세스만 열 수 있다 — MCP 서버(sh/run_mcp.sh)를 띄우기 '전에' 실행하고,
#    MCP 서버가 떠 있으면 먼저 끄고 실행할 것.
#
# 실행: bash sh/index_policies.sh
#
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x ".venv/bin/python3" ] && export PATH="$PWD/.venv/bin:$PATH"
if [ -f .env ]; then set -a; source .env; set +a; fi

if lsof -ti:8200 >/dev/null 2>&1; then
    echo "ERROR: MCP 서버(:8200)가 떠 있습니다 — 로컬 Qdrant 파일이 잠겨 있으니 먼저 종료하세요."; exit 1
fi

export POLICY_SEARCH=hybrid
export QDRANT_PATH="${QDRANT_PATH:-./qdrant_local}"
python3 scripts/index_policies.py --check "받은 신발이 찢어져 있어요"
