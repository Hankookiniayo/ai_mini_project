"""쇼핑몰 상담 에이전트 — MCP Server.

Tool / Resource / Prompt 3대 capability를 모두 노출하는 fastmcp 서버.

- Tool     list_my_orders / get_order / get_tracking : 주문·배송 상태는 계속 바뀌는
           '동적 조회'라 Tool이 맞다. 로그인한 고객(customer_id)의 주문만 조회된다
- Tool     search_products                           : 상품 가격·옵션 조회 (화면과 같은 상품 데이터)
- Tool     list_policies / search_policies           : 어떤 정책 문서가 있는지, 질문과 가장 가까운
           문서가 무엇인지 찾는다 (Qdrant RAG, 실패 시 키워드 폴백)
- Resource policy://shop/{topic}                     : URI로 식별되는 정적 정책 문서 본문
- Prompt   cs_consult_prompt                         : 상담 표준 절차(주문 확인 → 정책 확인 → 안내)

실행
    python mcp_server.py                                # stdio (server.py가 서브프로세스로 띄움)
    python mcp_server.py --transport http --port 8200   # 독립 HTTP 서비스 (sh/run_mcp.sh)
"""
import argparse

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from mcp_tools.policies import PolicySearcher
from mcp_tools.shop import ShopStore

mcp = FastMCP(name="shop-cs")

SHOP = ShopStore()                         # 프로세스당 1회 생성
POLICIES = PolicySearcher.from_env()


class OrderSummary(BaseModel):
    order_id: str
    ordered_at: str
    status: str = Field(description="paid / preparing / shipping / delayed / delivered / return_requested")
    status_label: str = Field(description="화면에 보이는 한글 상태")
    item_names: list[str]
    categories: list[str] = Field(default_factory=list, description="상품 카테고리 (의류·신발 등)")
    total_amount: int = Field(description="결제 금액 (원)")
    issue: str | None = Field(default=None, description="상담이 필요한 문제(지연·반품 진행 등), 없으면 null")


class OrderList(BaseModel):
    as_of: str = Field(description="기준일 (오늘)")
    orders: list[OrderSummary]


class PolicyHit(BaseModel):
    topic: str
    title: str
    score: float = Field(description="유사도 점수 (0~1, 높을수록 관련)")
    uri: str
    vector: float | None = Field(default=None, description="hybrid일 때: 벡터 유사도")
    keyword: float | None = Field(default=None, description="hybrid일 때: 키워드 점수")


class PolicySearchResult(BaseModel):
    query: str
    method: str = Field(description="hybrid | qdrant | keyword | keyword (폴백 사유)")
    hits: list[PolicyHit]


class PolicyCatalog(BaseModel):
    policies: list[dict]


# customer_id는 에이전트 서버가 로그인 세션에서 꺼내 채운다 — LLM이 만드는 인자가 아니다.
# (실서비스라면 MCP 요청의 인증 토큰에서 고객을 꺼내는 편이 더 안전하다. 목업에서는 인자로 넘긴다.)
@mcp.tool()
def list_my_orders(customer_id: str) -> OrderList:
    """로그인한 고객의 주문 요약 목록(최근 주문 먼저)을 반환합니다. 문제가 있는 주문은 issue에 표시됩니다."""
    return OrderList(**SHOP.list_orders(customer_id))


@mcp.tool()
def get_order(customer_id: str, order_id: str) -> dict:
    """주문 상세(상품·옵션·결제 금액·배송지·예정일·반품 정보·수령 후 경과일)를 조회합니다.
    없는 주문번호이거나 다른 고객의 주문이면 found=false를 반환합니다."""
    return SHOP.get_order(customer_id, order_id)


@mcp.tool()
def get_tracking(customer_id: str, order_id: str) -> dict:
    """주문의 배송 추적 이벤트(시각·위치·상태)와 마지막 이동 후 경과일을 조회합니다."""
    return SHOP.get_tracking(customer_id, order_id)


@mcp.tool()
def search_products(query: str) -> dict:
    """질문에 나온 상품명·카테고리와 맞는 상품의 판매가·옵션을 조회합니다.
    일치하는 상품이 없으면 found=false를 반환합니다 (가격을 추측하지 말 것)."""
    return SHOP.search_products(query)


@mcp.tool()
def list_policies() -> PolicyCatalog:
    """사용 가능한 정책 문서 카탈로그(topic, 제목, 키워드)를 반환합니다."""
    return PolicyCatalog(policies=[p.meta() for p in POLICIES.catalog])


@mcp.tool()
def search_policies(query: str, top_k: int = 3) -> PolicySearchResult:
    """고객 질문·주문 상황과 가장 가까운 정책 문서를 검색합니다 (벡터 + 키워드 하이브리드,
    Qdrant 실패 시 키워드 검색으로 폴백). 결과의 uri로 정책 Resource를 읽으면 본문을 얻습니다."""
    return PolicySearchResult(**POLICIES.search(query, top_k))


@mcp.resource("policy://shop/{topic}")
def get_policy(topic: str) -> str:
    """정책 문서 본문을 반환합니다 (머리말 메타데이터 제외)."""
    policy = POLICIES.by_topic.get(topic)
    if policy is None:
        return f"# Policy Not Found\n\nNo policy for topic: {topic}"
    return policy.body


@mcp.prompt()
def cs_consult_prompt(order_id: str, question: str) -> str:
    """상담 표준 절차 프롬프트 — planning 노드가 계획을 세울 때 이 절차를 따른다."""
    return f"""다음 고객 문의에 대해 상담 표준 절차를 수행하세요.

## 문의 정보
- 관련 주문: {order_id}
- 고객 질문: {question}

## 상담 절차 (반드시 순서대로 수행)
1. get_order / get_tracking Tool로 관련 주문의 실제 상태를 확인하세요.
   주문번호가 없으면 list_my_orders에서 질문과 관련된 주문(또는 문제가 있는 주문)을 찾으세요.
2. 질문과 주문 상황에 맞는 정책 문서(policy://shop/...)를 확인하세요.
3. 확인한 주문 정보와 정책만으로 안내하세요. 정책에 없는 기간·금액은 말하지 마세요.
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="shop CS MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8200)
    args = parser.parse_args()
    if args.transport == "http":
        # Streamable HTTP — 엔드포인트는 http://{host}:{port}/mcp
        mcp.run(transport="http", host=args.host, port=args.port, show_banner=False)
        return
    try:
        mcp.run(show_banner=False)
    except TypeError:   # 구버전 fastmcp 호환
        mcp.run()


if __name__ == "__main__":
    main()
