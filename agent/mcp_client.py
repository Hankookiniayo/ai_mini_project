"""Wave 2 — MCP 서버를 subprocess(stdio)로 1회 기동하거나 HTTP로 접속해 재사용하는 클라이언트.

Wave 1의 교훈대로 서버(lifespan)에서 한 번만 연결을 만들고 모든 요청이 공유한다.
Wave 9 데모용으로, 프로세스 최초의 get_order 호출 1회는 일부러
asyncio.TimeoutError를 던진다(transient) — with_retry가 backoff 후 복구해야 한다.

연결 대상은 "ops"(mcp_server.py — 주문·상품 조회 tool + 정책 resource/검색 tool + 상담 prompt)다.
서버가 하나여도 resource/prompt/tool을 요청할 때는 어느 서버가 그걸 들고 있는지 미리 가정하지
않고, find_resource_template()/find_prompt()/find_tool()로 연결된 서버들을 조회(list)해
이름이 맞는 걸 찾은 뒤에야 그 서버로 요청한다 — 나중에 서버를 역할별로 나눠도 이 코드는 그대로다.

(예전에는 탐색을 시연하려고 아무 기능도 쓰지 않는 decoy 서버 mcp_server_docs.py를 같이 연결했는데,
쇼핑몰 주제와 관계가 없어서 삭제했다. 서버 목록은 아래 DEFAULT_SERVER_* 한 곳에서 관리한다.)

MCP 서버를 별도 서비스로 분리
- 기존: server.py가 MCP 서버를 stdio 서브프로세스로 직접 띄움 → 에이전트가 죽으면 MCP도
  같이 죽고, MCP 하나를 여러 에이전트가 공유할 수 없고, MCP 쪽 의존성(Qdrant 클라이언트,
  임베딩 호출 등)이 에이전트 프로세스에 그대로 딸려 들어온다.
- 개선: MCP_TRANSPORT=http면 미리 떠 있는 MCP 서버(sh/run_mcp.sh)에 URL로 접속만 한다
  (MCP 표준의 Streamable HTTP 전송). 연결 이후의 call_tool/read_resource/get_prompt
  코드는 stdio와 완전히 같다 — 전송 방식만 바뀌고 MCP 프로토콜은 그대로다.
- find_tool(): resource/prompt처럼 tool도 "어느 서버가 들고 있는지" 먼저 조회한
  뒤에 호출한다 (search_policies 같은 tool을 동적으로 찾기 위함).
"""
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from langsmith import traceable
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

try:   # mcp SDK 2.x
    from mcp.client.streamable_http import streamable_http_client as _http_client
except ImportError:   # mcp SDK 1.x
    from mcp.client.streamable_http import streamablehttp_client as _http_client

SERVER_DIR = Path(__file__).parent.parent
DEFAULT_SERVER_SCRIPTS = {
    "ops": SERVER_DIR / "mcp_server.py",
}
DEFAULT_SERVER_URLS = {   # sh/run_mcp.sh가 띄우는 주소
    "ops": "http://127.0.0.1:8200/mcp",
}


def _attr(obj, *names):
    """mcp SDK 버전에 따라 pydantic 필드명이 camelCase(예: resourceTemplates,
    uriTemplate, 구버전 1.x)와 snake_case(예: resource_templates, uri_template,
    신버전 2.x)로 갈린다 — 이 프로젝트 안에도 시스템 python3(mcp 1.28.1, camelCase)와
    .venv(mcp 2.2.0, snake_case)가 섞여 있어 실행 환경마다 바뀐다. 둘 다 시도해서
    설치된 버전과 무관하게 동작하게 한다."""
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    raise AttributeError(f"{type(obj).__name__!r} has none of {names!r}")


class MCPClient:
    def __init__(self, flaky_first_tool_call: bool = True,
                 server_scripts: dict[str, Path] | None = None,
                 server_urls: dict[str, str] | None = None):
        # server_urls가 주어지면 HTTP(별도 서비스) 모드, 아니면 기존 stdio 모드
        self._server_urls = server_urls
        self._server_scripts = server_scripts or DEFAULT_SERVER_SCRIPTS
        self.transport = "http" if server_urls else "stdio"
        self._stack: AsyncExitStack | None = None
        self.sessions: dict[str, ClientSession] = {}
        self._lock = asyncio.Lock()
        self._flaky_pending = flaky_first_tool_call   # Wave 9 시뮬레이션

    @classmethod
    def from_env(cls) -> "MCPClient":
        """MCP_TRANSPORT=stdio(기본) | http. http면 MCP_OPS_URL로 접속."""
        if os.environ.get("MCP_TRANSPORT", "stdio") == "http":
            return cls(server_urls={
                "ops": os.environ.get("MCP_OPS_URL", DEFAULT_SERVER_URLS["ops"]),
            })
        return cls()

    async def start(self) -> None:
        self._stack = AsyncExitStack()
        if self._server_urls:
            await self._start_http()
            return
        for server_name, script in self._server_scripts.items():
            params = StdioServerParameters(
                command=sys.executable, args=[str(script)],
                env={**os.environ, "FASTMCP_LOG_LEVEL": "ERROR"})
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self.sessions[server_name] = session

    async def _start_http(self) -> None:
        """이미 떠 있는 MCP 서버들에 HTTP로 접속한다. 서버가 안 떠 있으면 원인을 바로
        알 수 있게 안내 메시지를 붙여 실패시킨다 (조용히 stdio로 바꾸지 않는다)."""
        for server_name, url in self._server_urls.items():
            try:
                streams = await self._stack.enter_async_context(_http_client(url))
                read, write = streams[0], streams[1]   # SDK 1.x는 3-튜플, 2.x는 2-튜플
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
            except Exception as exc:
                await self._stack.aclose()
                raise RuntimeError(
                    f"MCP 서버 '{server_name}'({url})에 연결하지 못했습니다 — "
                    f"먼저 'bash sh/run_mcp.sh'로 MCP 서버를 띄웠는지 확인하세요. ({exc!r})"
                ) from exc
            self.sessions[server_name] = session

    async def stop(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self.sessions = {}

    @traceable(run_type="tool", name="mcp.call_tool")
    async def call_tool(self, name: str, arguments: dict, server: str = "ops") -> dict:
        if self._flaky_pending and name == "get_order":
            self._flaky_pending = False
            raise asyncio.TimeoutError("simulated transient timeout (Wave 9 demo)")
        async with self._lock:
            result = await self.sessions[server].call_tool(name, arguments)
        text = result.content[0].text if result.content else ""
        # [개선] tool 내부 오류(예: 데이터 파일 읽기 실패)는 isError=True로 온다 —
        # JSON 파싱 에러로 뭉개지지 않게 원래 메시지를 담아 명시적으로 올린다.
        if getattr(result, "isError", None) or getattr(result, "is_error", None):
            raise RuntimeError(f"MCP tool '{name}' 실패: {text}")
        return json.loads(text)

    @traceable(run_type="tool", name="mcp.list_tools")
    async def list_tools(self, server: str) -> list:
        async with self._lock:
            result = await self.sessions[server].list_tools()
        return result.tools

    @traceable(run_type="tool", name="mcp.find_tool")
    async def find_tool(self, tool_name: str) -> str:
        """연결된 서버들을 순서대로 조회해 tool_name을 제공하는 서버를 찾는다."""
        for server_name in self.sessions:
            tools = await self.list_tools(server_name)
            if any(t.name == tool_name for t in tools):
                return server_name
        raise ValueError(f"MCP tool not found on any connected server: {tool_name}")

    @traceable(run_type="tool", name="mcp.list_resource_templates")
    async def list_resource_templates(self, server: str) -> list:
        async with self._lock:
            result = await self.sessions[server].list_resource_templates()
        return _attr(result, "resourceTemplates", "resource_templates")

    @traceable(run_type="tool", name="mcp.find_resource_template")
    async def find_resource_template(self, template_name: str) -> tuple[str, str]:
        """연결된 서버들을 순서대로 조회해 template_name을 제공하는 서버를 찾는다."""
        for server_name in self.sessions:
            templates = await self.list_resource_templates(server_name)
            matched = next((t for t in templates if t.name == template_name), None)
            if matched is not None:
                return server_name, _attr(matched, "uriTemplate", "uri_template")
        raise ValueError(
            f"MCP resource template not found on any connected server: {template_name}")

    @traceable(run_type="tool", name="mcp.read_resource")
    async def read_resource(self, uri: str, server: str) -> str:
        async with self._lock:
            result = await self.sessions[server].read_resource(uri)
        return result.contents[0].text

    @traceable(run_type="tool", name="mcp.list_prompts")
    async def list_prompts(self, server: str) -> list:
        async with self._lock:
            result = await self.sessions[server].list_prompts()
        return result.prompts

    @traceable(run_type="tool", name="mcp.find_prompt")
    async def find_prompt(self, prompt_name: str) -> str:
        """연결된 서버들을 순서대로 조회해 prompt_name을 제공하는 서버를 찾는다."""
        for server_name in self.sessions:
            prompts = await self.list_prompts(server_name)
            if any(p.name == prompt_name for p in prompts):
                return server_name
        raise ValueError(
            f"MCP prompt not found on any connected server: {prompt_name}")

    @traceable(run_type="tool", name="mcp.get_prompt")
    async def get_prompt(self, name: str, arguments: dict, server: str = "ops") -> str:
        async with self._lock:
            result = await self.sessions[server].get_prompt(name, arguments)
        return result.messages[0].content.text
