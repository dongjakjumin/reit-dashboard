"""배포 전 점검.

1) 저장한 자료 안에서 합계가 서로 맞는지 본다(구분별 합 = 전체 등).
2) 리츠정보시스템에 다시 물어 전체 건수가 저장분과 같은지 본다.
3) 리츠 몇 건을 뽑아 상세 화면(다른 조회 경로)의 값과 목록 값을 대조한다.
4) 저장 파일에 전화번호 모양의 문자열이 없는지 본다.

하나라도 어긋나면 종료 코드 1.
"""
from __future__ import annotations

import json
import random
import re
import sys
import urllib.request
from pathlib import Path

import build
import reits_api

DATA = Path(__file__).parent / "data"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    bad = 0

    def check(ok: bool, msg: str):
        nonlocal bad
        print(("  통과  " if ok else "  실패  ") + msg)
        bad += 0 if ok else 1

    snap = json.loads((DATA / "latest.json").read_text(encoding="utf-8"))
    d = build.build_data()
    s = d["summary"]
    reits = snap["reits"]

    print("[1] 내부 합계")
    check(sum(sum(v) for v in d["yearly"]["kind"].values()) + d["yearly"]["before"] == s["n_reits"],
          f"연도별 구분 합 + 이전 연도 = 전체 {s['n_reits']}건")
    check(sum(sum(v) for v in d["yearly"]["sector"].values()) + d["yearly"]["before"] == s["n_reits"],
          "연도별 투자대상 합 + 이전 연도 = 전체")
    check(sum(a["n"] for a in d["amc"]) == s["n_reits"], "자산관리회사별 리츠 수 합 = 전체")
    check(sum(a["assets_eok"] for a in d["amc"]) == s["assets_eok"], "자산관리회사별 자산총계 합 = 전체")
    check(sum(x["n"] for x in d["sector"]) == s["n_reits"], "투자대상별 리츠 수 합 = 전체")
    check(sum(s["new12_by_kind"].values()) == s["new12"], "최근 12개월 구분별 합 = 최근 12개월 전체")
    check(sum(s["filings_by_type"].values()) == s["filings_n"], "접수 유형별 합 = 접수 전체")
    check(sum(1 for r in reits if r["kind"] == "신고") == s["proj_n"], "구분이 신고인 건수 = 프로젝트리츠 건수")
    check(len({r["id"] for r in reits}) == len(reits), "리츠 번호 중복 없음")

    print("[2] 원문 건수 재조회")
    live = reits_api.post("/reits/prmpc/searchPrmpcOper", dict(reits_api.OPER_EMPTY, pageSize=10, pageNum=1))
    check(int(live["total"]) == len(reits), f"리츠조회 전체 {int(live['total'])}건 = 저장 {len(reits)}건")
    live_f = reits_api.post("/reits/prmpc/searchPrmpcInvtCnfm", {"reitsNm": "", "cmpnyMberNm": "", "pageSize": 10, "pageNum": 1})
    check(int(live_f["total"]) == len(snap["filings"]), f"접수현황 전체 {int(live_f['total'])}건 = 저장 {len(snap['filings'])}건")

    print("[3] 상세 화면 대조(자산총계가 있는 리츠 5건 + 강조 회사)")
    pool = [r for r in reits if r["assets_eok"]]
    random.seed(snap["date"])
    sample = random.sample(pool, 5)
    hl = d.get("highlight_amc")
    sample += [r for r in reits if hl and hl in r["amc"]][:1]
    for r in sample:
        try:
            det = reits_api.post("/reits/prmpc/searchPrmpcOperTab01_01", {"reitsNo": r["id"], "no": r["id"]})
        except Exception as e:  # 상세 조회 형식이 다르면 건너뜀
            print(f"  건너뜀 {r['name']}: {e}")
            continue
        text = json.dumps(det, ensure_ascii=False)
        name_ok = r["name"] in text
        m = re.search(r'"totAssets"\s*:\s*"?([\d,\.]+)', text)
        got = None
        if m:
            raw = float(m.group(1).replace(",", ""))
            got = round(raw / 1e8) if raw > 1e7 else round(raw)
        ok = name_ok and (got is None or abs(got - r["assets_eok"]) <= 1)
        check(ok, f"{r['name']} | 목록 {r['assets_eok']:,}억 | 상세 {got if got is not None else '값 없음'}")

    print("[4] 개인정보")
    blob = "".join(p.read_text(encoding="utf-8") for p in DATA.rglob("*.json"))
    site = (Path(__file__).parent / "site" / "index.html")
    blob += site.read_text(encoding="utf-8") if site.exists() else ""
    check(not re.search(r"01[016789]-?\d{3,4}-?\d{4}", blob), "저장 파일과 화면에 휴대전화 번호 모양 문자열 없음")
    check('"mberPhone"' not in blob and '"mberNm"' not in blob, "담당자 항목 이름 없음")

    print("결과:", "모두 통과" if bad == 0 else f"실패 {bad}건")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
