"""리츠정보시스템(reits.molit.go.kr) 공개 조회 화면과 같은 요청을 보내는 얇은 클라이언트.

- 표준 라이브러리만 쓴다(설치할 패키지 없음).
- 하루 1회 수집을 전제로 요청 사이에 간격을 둔다.
- 인증서 검증은 끄지 않는다. 사이트 인증서의 루트(OISTE WISeKey Global Root GB CA)가
  Windows 파이썬의 기본 저장소에 없을 수 있어 certs/ 폴더의 루트 인증서를 함께 읽는다.
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.request
from pathlib import Path

BASE = "https://reits.molit.go.kr/pub"
ROOT_CERT = Path(__file__).parent / "certs" / "oiste_wisekey_global_root_gb.pem"
USER_AGENT = "Mozilla/5.0 (compatible; reit-dashboard; personal research; once-daily)"
PAUSE_SECONDS = 2.5

_ctx = ssl.create_default_context()
if ROOT_CERT.exists():
    _ctx.load_verify_locations(str(ROOT_CERT))

_last_call = 0.0


def post(path: str, data: dict, retries: int = 3) -> dict:
    """조회 요청 1건. 응답의 data 부분을 돌려준다."""
    global _last_call
    body = json.dumps(
        {"token": None, "data": json.dumps(data, ensure_ascii=False)}, ensure_ascii=False
    ).encode("utf-8")
    req = urllib.request.Request(
        BASE + path,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Referer": BASE + "/main/mainPage",
        },
    )
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        wait = PAUSE_SECONDS - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=40, context=_ctx) as r:
                payload = json.loads(r.read().decode("utf-8"))
            _last_call = time.time()
            if payload.get("code") != "SCC":
                raise RuntimeError(f"{path}: code={payload.get('code')} msg={payload.get('msg')}")
            return payload["data"]
        except Exception as e:  # 네트워크 오류, 일시 차단 등
            _last_call = time.time()
            last_err = e
            time.sleep((5, 15, 30, 30)[min(attempt, 3)])
    raise RuntimeError(f"{path} 조회 실패: {last_err}")


def fetch_all(path: str, params: dict, page_size: int = 200) -> list[dict]:
    """페이지를 넘기며 목록 전체를 받는다. total과 받은 건수가 다르면 오류."""
    rows: list[dict] = []
    page = 1
    total = None
    while True:
        data = post(path, dict(params, pageSize=page_size, pageNum=page))
        total = int(data.get("total") or 0)
        chunk = data.get("list") or []
        rows.extend(chunk)
        if not chunk or len(rows) >= total:
            break
        page += 1
        if page > 100:
            raise RuntimeError(f"{path}: 페이지가 100을 넘음")
    if total is not None and len(rows) != total:
        raise RuntimeError(f"{path}: total={total}인데 {len(rows)}건을 받음")
    return rows


OPER_EMPTY = {
    "reitsNm": "", "mberNm": "", "invtTrgetCode": "", "pssrpCode": "",
    "totAssets1": "", "totAssets2": "", "ratio1": "", "ratio2": "",
    "reitsTyCode": "", "confmDe1": "", "confmDe2": "",
}


def reits_list() -> list[dict]:
    """운영 중 리츠 목록(리츠조회 화면)."""
    return fetch_all("/reits/prmpc/searchPrmpcOper", OPER_EMPTY)


def filings_list() -> list[dict]:
    """접수현황(영업인가, 등록, 프로젝트리츠 설립신고, 변경 신청)."""
    return fetch_all("/reits/prmpc/searchPrmpcInvtCnfm", {"reitsNm": "", "cmpnyMberNm": ""}, page_size=100)


def listed_list() -> list[dict]:
    """상장리츠 현황."""
    return fetch_all("/invt/lsted/searchLstedReits", {}, page_size=100)


def listed_metrics() -> list[dict]:
    """상장리츠 성과지표(결산기 기준 배당률, NAV, FFO). 리츠통계 > 상장리츠현황 화면."""
    return post("/chart/searchChart02_6", {})


def listed_wale() -> list[dict]:
    """상장리츠 WALE(첫 화면의 WALE 탭)."""
    return post("/main/searchMainStock", {"stockTab": "5"})


def price_history(code: str, date_from: str, date_to: str) -> list[dict]:
    """종목 1개의 일별 종가. 날짜는 YYYYMMDD."""
    return post("/main/searchStockChart", {"stockCd": code, "fromDt": date_from, "toDt": date_to})
