#!/usr/bin/env bash
#
# 하이브리드 LLM + 별도 MCP 서비스로 에이전트를 기동한다.
#   - 가벼운 대화(chat)        : Ollama 로컬 모델 (OLLAMA_CHAT_MODEL, 기본 qwen3:8b) — 과금 0
#   - route/plan/analyze      : OpenAI gpt-4o-mini
#   - write(최종 상담 답변)     : OpenAI gpt-4o
#   - MCP                     : sh/run_mcp.sh로 미리 띄운 서버에 HTTP로 접속
#
# ⚠️ route/plan/write는 실제 OpenAI API 호출이라 과금된다.
# 종료: 이 터미널에서 Ctrl+C
#
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x ".venv/bin/python3" ] && export PATH="$PWD/.venv/bin:$PATH"

if [ ! -f .env ]; then
    echo "ERROR: .env가 없습니다 — cp .env.example .env 후 키를 채우세요."; exit 1
fi
set -a; source .env; set +a
export LANGCHAIN_PROJECT="shop-cs-agent-hybrid"   # LangSmith 프로젝트를 실행 방식별로 분리

if [ -z "${OPENAI_API_KEY:-}" ] || [ "$OPENAI_API_KEY" = "sk-..." ]; then
    echo "ERROR: OPENAI_API_KEY가 비어 있습니다 (.env 확인)."; exit 1
fi

CHAT_MODEL="${OLLAMA_CHAT_MODEL:-qwen3:8b}"
if ! curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    echo "⚠️  Ollama가 꺼져 있습니다 — 가벼운 대화는 OpenAI로 폴백됩니다 (ollama serve로 켤 수 있음)."
elif ! curl -s http://127.0.0.1:11434/api/tags | grep -q "\"$CHAT_MODEL\""; then
    echo "⚠️  Ollama에 $CHAT_MODEL 모델이 없습니다 — ollama pull $CHAT_MODEL"
fi

if ! curl -s -o /dev/null http://127.0.0.1:8200/mcp; then
    echo "ERROR: MCP 서버(:8200)가 떠 있지 않습니다 — 새 터미널에서 bash sh/run_mcp.sh 먼저 실행"; exit 1
fi

echo "모델: chat=ollama:$CHAT_MODEL · plan=${OPENAI_FAST_MODEL:-gpt-4o-mini} · write=${OPENAI_QUALITY_MODEL:-gpt-4o}"
read -r -p "OpenAI API 과금이 발생합니다. 계속하시겠습니까? [y/N] " CONFIRM
case "$CONFIRM" in [yY]|[yY][eE][sS]) ;; *) echo "중단합니다."; exit 1 ;; esac

lsof -ti:8100 | xargs kill -9 2>/dev/null || true
cleanup() { echo; echo "에이전트를 종료합니다..."; lsof -ti:8100 | xargs kill -9 2>/dev/null || true; }
trap cleanup EXIT

LLM_BACKEND=hybrid MCP_TRANSPORT=http SUPERVISOR_VERSION=v2 python3 server.py &
SERVER_PID=$!
for _ in $(seq 1 40); do curl -s http://127.0.0.1:8100/health >/dev/null 2>&1 && break; sleep 0.5; done
echo; curl -s http://127.0.0.1:8100/health | python3 -m json.tool --no-ensure-ascii

cat <<'EOF'

################################################################
 에이전트가 떠 있습니다 (LLM_BACKEND=hybrid, MCP_TRANSPORT=http)
################################################################
 0) 목업회원 1(sol)로 로그인 — 세션 쿠키를 저장해 두고 아래 요청에 붙인다
    curl -s -c /tmp/shop.cookie -X POST localhost:8100/api/auth/login -H 'Content-Type: application/json' -d '{"login_id":"sol","password":"mock1234"}'
 1) 가벼운 대화 (Ollama가 답함)
    curl -s -b /tmp/shop.cookie -X POST localhost:8100/chat -H 'Content-Type: application/json' \
      -d '{"message":"안녕, 너는 누구야?","thread_id":"t1"}' | python3 -m json.tool --no-ensure-ascii
 2) 주문 상담 (OpenAI가 계획·답변 작성)
    curl -s -b /tmp/shop.cookie -X POST localhost:8100/chat -H 'Content-Type: application/json' \
      -d '{"message":"택배가 3일째 안 와요","thread_id":"t2"}' \
      | python3 -m json.tool --no-ensure-ascii
 3) 쇼핑몰 화면: (새 터미널) bash sh/run_frontend.sh → http://localhost:8600
EOF

wait "$SERVER_PID"
