"""web_demo_server.py — 쇼핑몰 화면(web_shop.html)을 띄우고 백엔드로 요청을 중계하는 얇은 레이어.

- web_shop.html 한 장을 같은 출처(origin)로 띄우고
- /api/shop/*(상품·주문 조회), /chat/stream(챗봇)을 백엔드 server.py(8100)로 그대로 중계(프록시)해서
브라우저의 CORS 제약 없이 화면과 에이전트가 같은 백엔드를 쓰게 한다.
web_shop.html은 Vue 3(CDN 글로벌 빌드, 빌드 단계 없음)로 되어 있다.

server.py는 평소처럼(fake/ollama/openai/hybrid 아무 백엔드로나) 8100에 미리 띄워 놓아야 한다.

실행:
    python3 web_demo_server.py        # http://localhost:8600 에서 쇼핑몰 화면 오픈
"""
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

BACKEND = "http://127.0.0.1:8100"
HERE = Path(__file__).parent

app = FastAPI()


@app.get("/")
async def index():
    return FileResponse(HERE / "web_shop.html")


@app.get("/health")
async def proxy_health():
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{BACKEND}/health")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/api/shop/{path:path}")
async def proxy_shop(path: str):
    """화면용 조회 API(상품·주문·고객) 중계."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{BACKEND}/api/shop/{path}")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/chat")
async def proxy_chat(req: Request):
    body = await req.body()
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(f"{BACKEND}/chat", content=body,
                              headers={"content-type": "application/json"})
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/chat/stream")
async def proxy_stream(message: str, thread_id: str = "web-demo"):
    async def relay():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "GET", f"{BACKEND}/chat/stream",
                params={"message": message, "thread_id": thread_id},
            ) as upstream:
                async for chunk in upstream.aiter_bytes():
                    yield chunk

    return StreamingResponse(relay(), media_type="text/event-stream")


if __name__ == "__main__":
    print("브라우저에서 http://localhost:8600 을 열어보세요 (server.py가 8100에 떠 있어야 합니다)")
    uvicorn.run(app, host="127.0.0.1", port=8600)
