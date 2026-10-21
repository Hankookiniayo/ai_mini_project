#!/usr/bin/env bash
#
# 과금 없는 fake LLM으로 에이전트를 띄운다 (화면·파이프라인 동작 확인용).
#   - LLM은 결정론적 가짜 응답 (agent/fake_llm.py) — 첫 답변은 일부러 짧게 써서 검수 반려를 보여준다
#   - MCP는 sh/run_mcp.sh로 띄운 서버가 있으면 HTTP로, 없으면 stdio 서브프로세스로 직접 띄운다
#
# 종료: 이 터미널에서 Ctrl+C
#
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x ".venv/bin/python3" ] && export PATH="$PWD/.venv/bin:$PATH"

if curl -s -o /dev/null http://127.0.0.1:8200/mcp; then MCP_TRANSPORT=http
else MCP_TRANSPORT=stdio; export POLICY_SEARCH="${POLICY_SEARCH:-keyword}"; fi

lsof -ti:8100 | xargs kill -9 2>/dev/null || true
cleanup() { echo; echo "에이전트를 종료합니다..."; lsof -ti:8100 | xargs kill -9 2>/dev/null || true; }
trap cleanup EXIT

# .env가 있어도 LLM은 fake로 고정한다 (과금 방지). LangSmith 추적은 .env 설정을 따른다.
if [ -f .env ]; then set -a; source .env; set +a; fi
export LANGCHAIN_PROJECT="shop-cs-agent-fake"   # LangSmith 프로젝트를 실행 방식별로 분리
LLM_BACKEND=fake MCP_TRANSPORT=$MCP_TRANSPORT SUPERVISOR_VERSION=v2 python3 server.py &
SERVER_PID=$!
for _ in $(seq 1 40); do curl -s http://127.0.0.1:8100/health >/dev/null 2>&1 && break; sleep 0.5; done
echo; curl -s http://127.0.0.1:8100/health | python3 -m json.tool --no-ensure-ascii

cat <<'MSG'

################################################################
 에이전트가 떠 있습니다 (LLM_BACKEND=fake — 과금 없음)
################################################################
 1) 상담 질문 (목업회원 1로 로그인한 뒤 세션 쿠키를 붙여 보낸다)
    curl -s -c /tmp/shop.cookie -X POST localhost:8100/api/auth/login -H 'Content-Type: application/json' -d '{"login_id":"sol","password":"mock1234"}'
    curl -sN -b /tmp/shop.cookie -G localhost:8100/chat/stream --data-urlencode "message=택배가 3일째 안 와요"
 2) 쇼핑몰 화면: (새 터미널) bash sh/run_frontend.sh → http://localhost:8600
MSG

wait "$SERVER_PID"
