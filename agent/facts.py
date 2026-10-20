"""'수집된 사실' 텍스트를 만드는 단일 함수.

write 노드(LLM)가 답변을 쓸 때 보는 사실과, validate 노드가 "답변의 숫자·날짜·주문번호가
사실에 있는가"를 검사할 때 쓰는 사실이 '똑같은 텍스트'여야 공정하다. 그래서 포맷팅을
여기 한 곳에 모았다 (교재 14장 모듈화). openai/ollama/fake LLM 모두 이 함수를 쓴다.
"""


def won(amount) -> str:
    """89000 → '89,000원' (답변에 나올 법한 표기로 사실을 적어 둔다)."""
    return f"{int(amount):,}원"


def _option(opt: dict) -> str:
    return "/".join(str(v) for v in opt.values()) if opt else "옵션 없음"


def format_order(d: dict) -> str:
    if not d.get("found", True):
        return f"- 주문 {d['order_id']}: 조회되지 않음 (고객 주문 목록에 없는 주문번호)"
    items = ", ".join(f"{i['name']}({_option(i.get('option', {}))}) {i['qty']}개 {won(i['price'])}"
                      for i in d["items"])
    line = (f"- 주문 {d['order_id']}: 상태={d['status_label']}, 주문일시 {d['ordered_at']}, "
            f"상품 {items}, 결제 금액 {won(d['total_amount'])}({d['payment_method']}), "
            f"배송비 {won(d['shipping_fee'])}, 도착 예정일 {d['expected_delivery']}, "
            f"배송지 {d['shipping_address']}")
    if d.get("delivered_at"):
        line += f", 배송 완료 {d['delivered_at']}"
    if d.get("days_since_delivery") is not None:
        open_ = "신청 가능" if d.get("return_window_open") else "신청 기간 지남"
        line += (f", 수령 후 {d['days_since_delivery']}일 경과, "
                 f"반품·교환 신청 마감일 {d['return_deadline']}({open_})")
    ret = d.get("return")
    if ret:
        line += (f", 반품 정보: 신청일 {ret['requested_at']}, 사유 '{ret['reason']}', "
                 f"회수 완료 {ret['pickup_done_at']}, 검수 상태 {ret['inspection']}, "
                 f"반품 배송비 {won(ret['return_shipping_fee'])}, "
                 f"예상 환불 금액 {won(ret['expected_refund_amount'])}")
    if d.get("issue"):
        line += f", 문제: {d['issue']}"
    return line


def format_tracking(t: dict | None) -> str | None:
    if not t or not t.get("found"):
        return None
    if not t["events"]:
        return f"  배송 추적 {t['order_id']}: 아직 출고 전이라 추적 기록 없음"
    events = " → ".join(f"{e['time']} {e['location']} {e['status']}" for e in t["events"])
    carrier = f"{t['carrier']} {t['tracking_no']}" if t.get("carrier") else "택배사 미배정"
    return (f"  배송 추적 {t['order_id']} ({carrier}): {events} "
            f"(마지막 기록 후 {t['days_since_last_event']}일 경과)")


def format_products(data: dict) -> str:
    if not data.get("found"):
        return (f"- 상품 검색 '{data['query']}': 일치하는 상품 없음 — 가격·옵션을 추측하지 말 것")
    lines = []
    for p in data["products"]:
        opts = ", ".join(f"{k} {'/'.join(v)}" for k, v in p.get("options", {}).items()) or "옵션 없음"
        lines.append(f"- 상품 {p['product_id']} {p['name']} ({p['category']}): "
                     f"판매가 {won(p['price'])}, {opts}")
    return "\n".join(lines)


def format_facts(collected: list[dict]) -> str:
    lines = []
    as_of = next((c["data"].get("as_of") for c in collected
                  if c["source"] == "order" and c["data"].get("as_of")), None)
    if as_of:
        lines.append(f"- 오늘 날짜: {as_of}")
    for c in collected:
        src = c["source"]
        if src == "order":
            lines.append(format_order(c["data"]))
            tracking = format_tracking(c.get("tracking"))
            if tracking:
                lines.append(tracking)
        elif src == "policy":
            # 어떤 주문(또는 질문) 때문에 어떤 정책이 골라졌는지 명시
            lines.append(f"- 정책 발췌 ({c['key']} → {c['topic']}, 유사도 {c['score']}):\n"
                         f"{c['data'].strip()}")
        elif src == "policy_ref":
            lines.append(f"- {c['key']}에도 위 정책({c['topic']})이 해당됨 (유사도 {c['score']})")
        elif src == "product":
            lines.append(format_products(c["data"]))
        elif src == "analysis":
            lines.append(f"- 주문 비교 분석: {c['data'].strip()}")
        elif src == "error":
            lines.append(f"- 조회 실패 ({c.get('target')}): {c['error']} — 이 항목은 확인되지 않음")
    lines += _missing_policy_lines(collected)
    return "\n".join(lines)


NO_POLICY_MARK = "정책 문서를 찾지 못함"


def _missing_policy_lines(collected: list[dict]) -> list[str]:
    """정책 검색이 끝내 실패한 주문(또는 질문)을 사실에 '명시'한다.

    [2차 실측 후 추가] 정책 발췌가 아예 없으면 작성 LLM은 '정책이 없다'는 걸 모른 채 그럴듯한
    기준("환불은 영업일 기준 7일 이내" — 실제는 3영업일)을 지어냈다. 지어낸 값 4건이 전부 이
    경로였다. 없다는 사실 자체를 사실 목록에 넣어 추측하지 말라고 알려준다."""
    covered = {c["key"] for c in collected if c["source"] in ("policy", "policy_ref")}
    missing = list(dict.fromkeys(c["key"] for c in collected
                                 if c["source"] == "policy_miss" and c["key"] not in covered))
    return [f"- {key}: 이 문의에 맞는 {NO_POLICY_MARK} — 기간·금액·조건을 추측해서 안내하지 말 것"
            for key in missing]


def safe_answer(collected: list[dict]) -> str:
    """검수에 끝까지 통과하지 못했을 때 내보내는 '안전한 기본 답변'.

    [2차 실측 후 추가] 예전에는 3번 반려돼도 마지막 초안을 '통과 처리'해서, 지어낸 기간이 든
    답이 고객에게 그대로 나갔다. 상담에서는 틀린 기준을 말하는 것보다 모른다고 하고 사람에게
    넘기는 게 낫다. 이 답변은 LLM 없이 수집된 사실(주문번호·상태)만으로 만들어서 검증 규칙을
    항상 통과한다."""
    confirm = []
    for c in collected:
        if c["source"] != "order":
            continue
        d = c["data"]
        if not d.get("found", True):
            confirm.append(f"{d['order_id']}는 고객님 주문 내역에서 확인되지 않습니다")
        else:
            items = ", ".join(i["name"] for i in d["items"])
            confirm.append(f"{d['order_id']}({items})는 현재 {d['status_label']} 상태입니다")
    for c in collected:   # 상품 가격도 조회된 값이면 그대로 알려준다
        if c["source"] == "product":
            confirm += [f"{p['name']}의 판매가는 {won(p['price'])}입니다"
                        for p in c["data"].get("products", [])]
    first = "; ".join(confirm) or "특정 주문 없이 안내드립니다"
    return (f"1) 주문 확인: {first}.\n"
            "2) 정책 안내: 이 문의에 맞는 정확한 기준을 확인하지 못해, 기간이나 금액은 "
            "추측해서 안내드리지 않을게요.\n"
            "3) 다음 단계: 고객센터 1:1 문의로 남겨 주시면 담당자가 정확한 기준으로 답변드리겠습니다.")
