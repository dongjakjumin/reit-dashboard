"""하루 1회 실행하는 수집기.

리츠정보시스템에서 세 목록(운영 중 리츠, 접수현황, 상장리츠)을 받아
 - data/snapshots/날짜.json : 그날 받은 원자료(개인정보 항목 제외)
 - data/latest.json         : 가장 최근 수집분
 - data/filings_log.json    : 접수 건별 누적 기록(처음 본 날, 마지막으로 본 날, 진행상태 변화)
 - data/reits_log.json      : 리츠별 누적 기록(처음 본 날, 마지막으로 본 날)
 - data/prices.json         : 상장리츠 종목별 일별 종가
을 만든다.

접수현황은 현재 처리 중인 건만 보여 주므로, 날마다 쌓아 둔 기록이 있어야
"접수에서 인가까지 걸린 기간"을 계산할 수 있다.

개인정보: 접수현황 응답에는 신청 담당자 이름과 전화번호가 들어 있다.
아래 FILING_KEEP에 적은 항목만 저장하고 나머지는 버린다.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

import reits_api
from textutil import CHANGE_REQUEST, PHONE_RE, filing_result, mask_officer_names, mask_person, phone_files, redact_phones

KST = timezone(timedelta(hours=9))
DATA = Path(__file__).parent / "data"
SNAP = DATA / "snapshots"
FILING_RESULT_LOOKUP_DAYS = 90
MAX_CONSECUTIVE_PRICE_FAILURES = 3

REIT_KEEP = {
    "reitsNo": "id", "reqstNo": "reqst_no", "reitsNm": "name", "mberNm": "amc",
    "cnfmAtNm": "kind", "prjctReitsAt": "project", "confmDe": "date",
    "reitsTyNm": "type", "invtTrgetNm": "sector", "pssrpNm": "public_offering",
    "totAssets": "assets_eok", "ratio": "dividend_pct", "lstDe": "listed_date",
}
FILING_KEEP = {
    "crrDetailNo": "id", "estateInvtCmpnyNo": "reit_id", "rceptDe": "date",
    "reqstTyNm": "req_type", "reitsNm": "name", "cmpnyMberNm": "applicant",
    "progrsSttusNm": "status", "bsnsNm": "project_name", "reitsBsnsTyNm": "biz_type",
    "fondCptl": "founding_capital", "pssrpDutyAt": "public_duty",
}
METRIC_KEEP = {
    "estateInvtCmpnyNo": "reit_id", "trgetCodeNm": "sector", "stacntRelmDe": "fiscal_end",
    "psacntNo": "fiscal_period", "stockDe": "fiscal_price_date", "nowPc": "fiscal_price",
    "dvdRate": "dvd_pct", "annuDvdRate": "annual_dvd_pct",
    "navThstrm": "nav_mil", "ffoThstrm": "ffo_mil",
}
LISTED_KEEP = {
    "stockCd": "code", "stockNm": "name", "cmpnyNm": "company", "lstDt": "listed_date",
    "nowPc": "price", "beforePc": "prev_price", "mktcTotamt": "mktcap_eok",
    "tradeVolume": "volume", "hmpg": "homepage", "stockDtTitle1": "as_of",
}


def _num(v):
    """'10,300,000,000' 같은 문자열을 숫자로. 빈 값이나 숫자가 아닌 값은 None."""
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s == "":
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return int(f) if f == int(f) else f


def _pick(row: dict, keep: dict, numeric: tuple[str, ...]) -> dict:
    out = {new: row.get(old) for old, new in keep.items()}
    for k in numeric:
        out[k] = _num(out.get(k))
    for k, v in out.items():
        if isinstance(v, str):
            out[k] = v.strip()
    return out


def collect() -> dict:
    now = datetime.now(KST)
    reits = [_pick(r, REIT_KEEP, ("id", "reqst_no", "assets_eok", "dividend_pct"))
             for r in reits_api.reits_list()]
    filings = [_pick(r, FILING_KEEP, ("id", "reit_id", "founding_capital"))
               for r in reits_api.filings_list()]
    listed = [_pick(r, LISTED_KEEP, ("price", "prev_price", "mktcap_eok", "volume"))
              for r in reits_api.listed_list()]
    # 성과지표와 WALE을 종목코드, 종목명으로 붙인다
    metrics = {str(m.get("stockCd")): m for m in reits_api.listed_metrics()}
    wale = {str(w.get("stockNm")).strip(): _num(w.get("waleThstrm")) for w in reits_api.listed_wale()}
    for row in listed:
        m = metrics.get(str(row["code"]))
        extra = _pick(m, METRIC_KEEP, ("reit_id", "fiscal_price", "dvd_pct", "annual_dvd_pct",
                                       "nav_mil", "ffo_mil")) if m else {v: None for v in METRIC_KEEP.values()}
        row.update(extra)
        row["wale_years"] = wale.get(row["name"])
    if len(reits) < 300:
        raise RuntimeError(f"리츠 목록이 {len(reits)}건뿐임. 수집 이상으로 보고 저장하지 않음")
    if not filings or not listed:
        # 접수 목록이 빈 채로 저장되면 진행 중인 신청이 모두 '처리 끝남'으로 기록된다
        raise RuntimeError(f"접수 {len(filings)}건, 상장 {len(listed)}건. 빈 목록은 수집 이상으로 보고 저장하지 않음")
    return {
        "collected_at": now.strftime("%Y-%m-%d %H:%M KST"),
        "date": now.strftime("%Y-%m-%d"),
        "source": "국토교통부 리츠정보시스템(reits.molit.go.kr)",
        "reits": reits,
        "filings": filings,
        "listed": listed,
    }


def _load(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def _save(path: Path, obj, pretty: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=1 if pretty else None,
                      separators=None if pretty else (",", ":"))
    tmp = NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False)
    pending = Path(tmp.name)
    try:
        with tmp:
            tmp.write(text + "\n")
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def update_logs(snap: dict):
    today = snap["date"]

    # 접수 건별 누적 기록
    flog = _load(DATA / "filings_log.json", {})
    seen = set()
    for f in snap["filings"]:
        key = str(f["id"])
        seen.add(key)
        rec = flog.get(key)
        if rec is None:
            rec = dict(f, first_seen=today, last_seen=today,
                       status_history=[[today, f["status"]]], gone_on=None)
            flog[key] = rec
        else:
            if rec["status_history"][-1][1] != f["status"]:
                rec["status_history"].append([today, f["status"]])
            rec.update({k: v for k, v in f.items()})
            rec["last_seen"] = today
            rec["gone_on"] = None
    reit_by_id = {r["id"]: r for r in snap["reits"]}
    cutoff = (date.fromisoformat(today) - timedelta(days=FILING_RESULT_LOOKUP_DAYS)).isoformat()
    for key, rec in flog.items():
        if key in seen:
            continue
        if rec.get("gone_on") is None:
            rec["gone_on"] = today  # 목록에서 빠진 것을 처음 확인한 날
            rec.setdefault("result_kind", None)
            rec.setdefault("result_date", None)
        if (rec["req_type"] != CHANGE_REQUEST and rec.get("result_date") is None
                and rec["gone_on"] > cutoff):
            rec.update(filing_result(rec, reit_by_id.get(rec.get("reit_id"))))
    _save(DATA / "filings_log.json", flog, pretty=True)

    # 리츠별 누적 기록(목록에 새로 들어온 날, 빠진 날)
    rlog = _load(DATA / "reits_log.json", {})
    seen = set()
    for r in snap["reits"]:
        key = str(r["id"])
        seen.add(key)
        rec = rlog.get(key)
        if rec is None:
            rlog[key] = {"name": r["name"], "amc": r["amc"], "kind": r["kind"],
                         "date": r["date"], "first_seen": today, "last_seen": today,
                         "gone_on": None}
        else:
            rec.update({"name": r["name"], "amc": r["amc"], "kind": r["kind"],
                        "date": r["date"], "last_seen": today, "gone_on": None})
    for key, rec in rlog.items():
        if key not in seen and rec.get("gone_on") is None:
            rec["gone_on"] = today
    _save(DATA / "reits_log.json", rlog, pretty=True)


def update_prices(snap: dict):
    """종목별 일별 종가 누적. 기록이 짧은 종목은 최근 1년치를 한 번 받아 채운다."""
    path = DATA / "prices.json"
    prices = _load(path, {})
    today = datetime.strptime(snap["date"], "%Y-%m-%d")
    d_to = today.strftime("%Y%m%d")
    d_from = (today - timedelta(days=372)).strftime("%Y%m%d")
    failures = consecutive_failures = skipped = 0
    for row in snap["listed"]:
        code = str(row["code"])
        hist = prices.get(code, {})
        if consecutive_failures < MAX_CONSECUTIVE_PRICE_FAILURES:
            start = d_from if len(hist) < 150 else max(hist).replace("-", "")
            try:
                history = reits_api.price_history(code, start, d_to)
            except Exception as exc:
                failures += 1
                consecutive_failures += 1
                print(f"[수집] 경고 시세 {code}: {exc}")
            else:
                consecutive_failures = 0
                for p in history:
                    day = str(p.get("stockDt") or "")[:10]
                    price = _num(p.get("nowPc"))
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) and day < snap["date"] and price:
                        hist[day] = price
                prices[code] = hist
        else:
            skipped += 1
        stamp = row.get("as_of") or ""
        if stamp[:10] == snap["date"] and "15:40" <= stamp[11:16] <= "23:59" and row.get("price"):
            hist[snap["date"]] = row["price"]
        if hist or code in prices:
            prices[code] = dict(sorted(hist.items()))
    _save(path, prices)
    if failures:
        message = f"시세 이력 조회 실패 {failures}종목"
        if skipped:
            message += f", 조회 생략 {skipped}종목"
        raise RuntimeError(message)


FIN_REFRESH_DAYS = 7      # 상장리츠 재무 자료는 일주일에 한 번만 다시 받는다
DISCLOSURE_DAYS = 120     # 수시공시는 최근 120일을 받아 누적한다
# 임원, 자산운용전문인력 변경처럼 개인 이름이 주 내용인 공시는 내용 문구를 저장하지 않는다
DISCLOSURE_NO_TEXT = {"RT02100003", "RT02100015"}


def update_financials(snap: dict):
    """상장리츠의 자산, 부채, 자본과 결산기별 추이. 요청 수를 줄이려고 주 1회만 갱신."""
    path = DATA / "financials.json"
    fin = _load(path, {})
    today = datetime.strptime(snap["date"], "%Y-%m-%d")
    fetched = 0
    for row in snap["listed"]:
        rid = row.get("reit_id")
        if not rid:
            continue
        key = str(rid)
        old = fin.get(key)
        fresh = old and (today - datetime.strptime(old["fetched"], "%Y-%m-%d")).days < FIN_REFRESH_DAYS \
            and old.get("fiscal_end") == row.get("fiscal_end")
        if fresh:
            continue
        b = reits_api.reit_basic(rid)
        bal = reits_api.reit_balance_series(rid)
        inc = {x.get("stacntRelmDe"): x for x in reits_api.reit_income_series(rid)}
        series = []
        for x in bal:
            end = x.get("stacntRelmDe")
            label = " ".join(str(x.get("psacntQuNo") or "").split())
            i = inc.get(end, {})
            series.append({"end": end, "label": label,
                           "assets": _num(x.get("assetsSum")), "debt": _num(x.get("debtSum")),
                           "equity": _num(x.get("caplSum")),
                           "revenue": _num(i.get("ernSum")), "op_profit": _num(i.get("profitSum"))})
        series.sort(key=lambda z: z["end"] or "")
        fin[key] = {
            "fetched": snap["date"], "code": row["code"], "name": row["name"],
            "fiscal_end": row.get("fiscal_end"),
            "assets": _num(b.get("totAssets")), "debt": _num(b.get("cmpnyDebt")),
            "equity": _num(b.get("cmpnyCaplSm")), "capital": _num(b.get("cmpnyCapl")),
            "top_shareholder": mask_person(" ".join((b.get("shrholdrNm") or "").split())) or None,
            "base_year": b.get("baseYear"), "period": b.get("psacntNo"),
            "series": series,
        }
        fetched += 1
    _save(path, fin, pretty=True)
    return fetched


def update_disclosures(snap: dict):
    """수시공시 누적 기록. 공시번호를 키로 쌓고, 1년이 지난 건은 지운다."""
    path = DATA / "disclosures.json"
    log = _load(path, {})
    today = datetime.strptime(snap["date"], "%Y-%m-%d")
    rows = reits_api.disclosures((today - timedelta(days=DISCLOSURE_DAYS)).strftime("%Y%m%d"),
                                 today.strftime("%Y%m%d"))
    for x in rows:
        no = x.get("pblntfNo")
        if no is None:
            continue
        code = x.get("matterCode")
        matter = " ".join(str(x.get("matterNm") or "").split())
        text = "" if code in DISCLOSURE_NO_TEXT or "주주" in matter else " ".join(str(x.get("changeCn") or "").split())[:300]
        text = mask_officer_names(re.sub(PHONE_RE, "(번호 생략)", text))
        log[str(int(no))] = {
            "date": x.get("pblntfDe"), "company": (x.get("cmpnyNm") or "").strip(),
            "company_type": x.get("cmpnySeNm"), "matter_code": code,
            "matter": matter, "text": text,
        }
    cutoff = (today - timedelta(days=365)).strftime("%Y-%m-%d")
    log = {k: v for k, v in log.items() if (v.get("date") or "") >= cutoff}
    _save(path, dict(sorted(log.items(), key=lambda kv: (kv[1]["date"], int(kv[0])), reverse=True)))
    return len(rows)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    snap = redact_phones(collect())
    _save(SNAP / f"{snap['date']}.json", snap)
    _save(DATA / "latest.json", snap)
    update_logs(snap)
    from dart import update as update_dart
    from dart_notes import update as update_notes
    from ecos import update as update_rates
    for group, update in (("시세", update_prices), ("재무", update_financials), ("공시", update_disclosures), ("DART", update_dart), ("DART 주석", update_notes),
                          ("금리", update_rates)):
        try:
            update(snap)
        except Exception as exc:
            print(f"[수집] 경고 {group}: {type(exc).__name__} {exc}")
    leaks = phone_files(DATA.rglob("*.json"))
    if leaks:
        raise SystemExit("전화번호 확인 필요: " + ", ".join(str(p.relative_to(DATA)) for p in leaks))
    print(f"[수집] {snap['collected_at']} 리츠 {len(snap['reits'])}건, "
          f"접수 {len(snap['filings'])}건, 상장 {len(snap['listed'])}건")


if __name__ == "__main__":
    main()
