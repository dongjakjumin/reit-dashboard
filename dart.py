"""OpenDART 사업보고서에서 차입 잔액과 보통주 배당을 정리한다."""
import argparse
import calendar
import io
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUTPUT = DATA / "dart.json"
EXITS = DATA / "exits.json"
# 상장리츠 목록에서 빠진 종목의 사유를 보여 주는 공시(보고서명 기준). 신고인 이름은 저장하지 않는다
EXIT_TITLE = re.compile(r"해산사유|상장폐지|합병결정|분할결정|회생절차|파산")


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def api_key():
    key = os.environ.get("DART_API_KEY", "").strip()
    if not key and (ROOT / ".env").is_file():
        for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("DART_API_KEY="):
                return line.partition("=")[2].strip().strip("\"'")
    return key


class DartError(Exception):
    pass


class Source:
    def __init__(self, folder, key):
        self.folder = folder
        self.key = key

    def request(self, name, params, parse):
        for attempt in range(2):
            time.sleep(0.31)
            try:
                query = urllib.parse.urlencode(dict(params, crtfc_key=self.key))
                with urllib.request.urlopen("https://opendart.fss.or.kr/api/" + name + "?" + query,
                                            timeout=30) as response:
                    return parse(response.read())
            except Exception as exc:
                kind = type(exc).__name__
        raise DartError(kind) from None

    def companies(self):
        if self.folder:
            return read(self.folder / "corp.json")

        def parse(body):
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                root = ET.fromstring(archive.read("CORPCODE.xml"))
            return {r.findtext("stock_code").strip(): [r.findtext("corp_code"), r.findtext("corp_name")]
                    for r in root.findall("list") if r.findtext("stock_code", "").strip()}
        return self.request("corpCode.xml", {}, parse)

    def rows(self, code, corp, year, basis=None):
        stem = "fs" if basis else "alot"
        suffix = "_" + basis if basis else ""

        def rows(obj):
            if obj["status"] == "013":
                return []
            if obj["status"] != "000" or not obj.get("list"):
                raise DartError()
            return obj["list"]

        if self.folder:
            path = self.folder / f"{stem}_{code}_{year}_11011{suffix}.json"
            return rows(read(path)) if path.is_file() else []
        params = dict(corp_code=corp, bsns_year=year, reprt_code="11011")
        if basis:
            params["fs_div"] = basis
        return self.request("fnlttSinglAcntAll.json" if basis else "alotMatter.json",
                            params, lambda body: rows(json.loads(body)))


def number(value):
    if value is None or str(value).strip() in ("", "-"):
        return None
    value = str(value).strip().replace(",", "")
    return float(value) if "." in value else int(value)


def compact(value):
    return "".join(value.split())


def paid_interest_accounts(rows, field):
    accounts = []
    for row in rows:
        if row["sj_div"] != "CF":
            continue
        name = compact(row["account_nm"])
        account_id = row["account_id"]
        if not (account_id.startswith("ifrs-full_InterestPaid")
                or re.fullmatch(r"이자(?:의)?지급(?:\((?:영업|재무)(?:활동)?\))?", name)):
            continue
        amount = number(row.get(field))
        if amount is not None:
            accounts.append(dict(name=row["account_nm"], account_id=account_id, amount=amount))
    return accounts


def section_hints(name, account_id):
    hints = []
    if "비유동성부분" in name:
        hints.append(False)
    elif "유동성" in name or name.startswith(("유동", "단기", "전자단기")):
        hints.append(True)
    elif name.startswith(("장기", "비유동")):
        hints.append(False)
    key = account_id.split("_")[-1].lower()
    if key.startswith(("current", "shortterm")):
        hints.append(True)
    elif key.startswith(("noncurrent", "longterm")):
        hints.append(False)
    return hints


def statement(rows, term, notes):
    field = term + "_amount"
    bs = sorted((r for r in rows if r["sj_div"] == "BS"), key=lambda r: int(r["ord"]))
    balances = {}
    accounts = []
    section = None
    assets = None
    extra = {}  # 감정가 기준 NAV 계산에 쓰는 재무상태표 줄. 처음 나온 줄만 쓴다.
    for r in bs:
        name = compact(r["account_nm"])
        amount = number(r.get(field))
        if name == "자산총계" or r["account_id"] == "ifrs-full_Assets":
            # 차입 비율(LTV)의 분모. 처음 나온 자산총계 줄만 쓴다.
            if assets is None:
                assets = amount
            continue
        for key, ids, names in (("investment_property", ("ifrs-full_InvestmentProperty",), ("투자부동산",)),
                                ("equity", ("ifrs-full_Equity",), ("자본총계",)),
                                ("equity_owner", ("ifrs-full_EquityAttributableToOwnersOfParent",),
                                 ("지배기업소유주지분", "지배기업의소유주에게귀속되는자본", "지배기업소유주에게귀속되는자본")),
                                ("nci", ("ifrs-full_NoncontrollingInterests",), ("비지배지분",))):
            if key not in extra and (r["account_id"] in ids or name in names):
                extra[key] = amount
        if name in ("유동부채", "비유동부채", "부채총계"):
            section = {"유동부채": True, "비유동부채": False, "부채총계": None}[name]
            balances[section] = amount or 0
            continue
        if not re.search("차입금|사채|유동성장기부채", name) or re.search("보증금|리스|파생|기타금융부채|미지급|금융자산", name):
            continue
        hints = section_hints(name, r["account_id"])
        if section is None:
            # 자산 및 자본 구간의 사채 투자 등은 제외한다.
            continue
        if any(h != section for h in hints):
            notes.append(f"{term} {name} 계정 이름 또는 코드와 구간 불일치. 부채 구간 적용")
        accounts.append(dict(name=r["account_nm"], amount=amount, current=section))

    for current in (True, False):
        group = [a for a in accounts if a["current"] == current]
        parents = [a for a in group if "차입금및사채" in compact(a["name"]) and a["amount"] is not None]
        if len(parents) == 1 and len(group) > 1:
            for a in group:
                if a is not parents[0]:
                    a["included"] = False
                    notes.append(f"{term} {a['name']} 내역은 {parents[0]['name']}에 포함")
    debt = {}
    for current, key in ((True, "short"), (False, "long")):
        debt[key] = sum(a["amount"] or 0 for a in accounts if a["current"] == current and a.get("included", True))
    if None not in balances or True not in balances:
        raise ValueError("부채 구간 누락")
    debt.update(extra)
    debt.update(total=debt["short"] + debt["long"], liabilities=balances[None], assets=assets,
                current_liabilities=balances[True], noncurrent_liabilities=balances.get(False, balances[None]-balances[True]))
    if (debt["short"] > debt["current_liabilities"] or debt["long"] > debt["noncurrent_liabilities"]
            or debt["total"] > debt["liabilities"]):
        raise ValueError("차입 합계 확인 필요")
    income = [r for r in rows if r["sj_div"] in ("CIS", "IS") and number(r.get(field)) is not None]

    def pick(names, account_id):
        for name in names:
            for r in income:
                if compact(r["account_nm"]) == name:
                    return r
        return next((r for r in income if r["account_id"] == account_id), {})
    cost = pick(("이자비용",), "ifrs-full_InterestExpense") or pick(("금융비용", "금융원가"), "ifrs-full_FinanceCosts")
    op = pick(("영업이익", "영업이익(손실)"), "dart_OperatingIncomeLoss")
    revenue = pick(("영업수익", "수익(매출액)"), "ifrs-full_Revenue")
    cost_amount = number(cost.get(field))
    interest_accounts = paid_interest_accounts(rows, field)
    if interest_accounts:
        interest = sum(abs(a["amount"]) for a in interest_accounts)
        interest_source = "이자 지급액"
    else:
        interest = cost_amount
        interest_source = cost.get("account_nm")
    return dict(debt, accounts=accounts, cost=cost_amount, cost_name=cost.get("account_nm"),
                interest=interest, interest_source=interest_source, interest_accounts=interest_accounts,
                op=number(op.get(field)), revenue=number(revenue.get(field)))


def period_dates(series):
    result = {}
    for row in series:
        m = re.search(r"(\d+)기\s*결산기", row.get("label", ""))
        if m and row.get("end"):
            result[int(m[1])] = date.fromisoformat(row["end"])
    return result


def dates_in(value):
    return [date(int(y), int(m), int(d)) for y, m, d in re.findall(
        r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})", value or "")]


def previous_year(end):
    month_end = calendar.monthrange(end.year, end.month)[1]
    day = calendar.monthrange(end.year - 1, end.month)[1] if end.day == month_end else end.day
    return end.replace(year=end.year - 1, day=day)


def summarize(fs, basis, reports, series):
    notes = []
    dates = period_dates(series)
    r = fs[0]
    periods = {}
    for term in ("thstrm", "frmtrm"):
        match = re.search(r"(\d+)\s*기", r.get(term + "_nm", ""))
        periods[term] = int(match[1]) if match else None
    grouped = defaultdict(list)
    for row in reports:
        grouped[row["rcept_no"]].append(row)
    ends = {no: date.fromisoformat(rows[0]["stlm_dt"]) for no, rows in grouped.items()}
    if periods["thstrm"] and r["rcept_no"] in ends:
        dates[periods["thstrm"]] = ends[r["rcept_no"]]
    all_ends = sorted(set(dates.values()) | set(ends.values()))
    financial = {}
    for term, key in (("thstrm", "current"), ("frmtrm", "previous")):
        period = periods[term]
        spans = [dates_in(row.get(term + "_dt")) for row in fs]
        span = max(spans, key=len)
        end = span[-1] if span else dates.get(period)
        prior = dates.get(period-1) if period else None
        start = span[0] if len(span) >= 2 else prior + timedelta(days=1) if prior else None
        result = statement(fs, term, notes)
        result.update(end=str(end) if end else None, start=str(start) if start else None,
                      days=(end-start).days+1 if start and end and start <= end else None)
        financial[key] = result
    # 기수가 확인되는 두 말일 사이에 누락된 기수가 있으면 여러 기를 한 기로 세지 않는다.
    numbers = {end: period for period, end in dates.items()}
    lengths = {}
    for prior, end in zip(all_ends, all_ends[1:]):
        if end in numbers and prior in numbers and numbers[end] != numbers[prior] + 1:
            continue
        lengths[end] = (end - prior).days
    for s in financial.values():
        if s["days"] and s["end"]:
            lengths[date.fromisoformat(s["end"])] = s["days"]
    dividends = {}
    dividend_fields = ("thstrm", "frmtrm", "lwfr")
    for no in sorted(grouped):
        index = all_ends.index(ends[no])
        per_share = [x for x in grouped[no] if compact(x["se"]) == "주당현금배당금(원)"]
        common = [x for x in per_share if compact(x.get("stock_knd") or "") in ("보통주", "보통주식")]
        if not common:
            numeric_rows = [x for x in per_share if any(number(x.get(field)) is not None for field in dividend_fields)]
            if len(numeric_rows) == 1 and not re.search("우선|종류|전환", compact(numeric_rows[0].get("stock_knd") or "")):
                common = numeric_rows
                notes.append(f"{no} 보통주 표시가 없어 숫자가 있는 유일한 주당 현금배당금 줄을 보통주로 처리")
        if not common:
            notes.append(f"{no} 보통주 주당 현금배당금 표시 없음")
            continue
        for offset, field in enumerate(dividend_fields):
            if index < offset:
                continue
            end = all_ends[index-offset]
            if any(day not in lengths for day in all_ends[index-offset+1:index+1]):
                continue
            value = number(common[0].get(field))
            old = dividends.get(end)
            if old and old["dps"] != value:
                notes.append(f"{end} 배당 {old['report']} {old['dps']} → {no} {value}원")
            dividends[end] = dict(end=str(end), days=lengths.get(end), dps=value, report=no)
    latest = max(ends.values(), default=None)
    trailing = None
    if latest:
        cutoff = previous_year(latest)
        periods = [x for end, x in dividends.items() if cutoff < end <= latest]
        if (latest in dividends and all(x["dps"] is not None and x["days"] for x in periods)
                and sum(x["days"] for x in periods) == (latest-cutoff).days):
            trailing = sum(x["dps"] for x in periods)
    return dict(financial, basis=basis, rcept_no=r["rcept_no"], dividends=list(sorted(dividends.values(), key=lambda v:v["end"])),
                dividend_end=str(latest) if latest else None, trailing_dps=trailing, notes=notes)


def update(snap=None, folder=None, force=False):
    key = None if folder else api_key()
    if not folder and not key:
        print("DART 인증키가 없어 건너뜀")
        return
    snap = snap or read(DATA / "latest.json")
    old = read(OUTPUT) if OUTPUT.exists() else {}
    today = date.today()
    due = [r for r in snap["listed"] if folder or force or (today-date.fromisoformat(
        old.get(r["code"], {}).get("fetched", "2000-01-01"))).days >= 7]
    if not due:
        return
    source = Source(folder, key)
    corps = source.companies()
    fin = read(DATA / "financials.json")
    changed = 0
    for stock in due:
        code = stock["code"]
        try:
            corp = corps[code][0]
            reports = []
            chosen = None
            for year in (today.year, today.year - 1):
                reports.extend(source.rows(code, corp, year))
                for basis in ("CFS", "OFS"):
                    fs = source.rows(code, corp, year, basis)
                    if fs:
                        if chosen is None:
                            chosen = fs, basis
                        break
            if not chosen:
                raise ValueError("재무제표 없음")
            record = summarize(*chosen, reports, fin.get(str(stock["reit_id"]), {}).get("series", []))
            record.update(name=stock["name"], fetched=today.isoformat(), corp_code=corp)
            record_text = json.dumps(record, ensure_ascii=False)
            if (key and key in record_text) or re.search(r"[a-fA-F0-9]{40}", record_text):
                raise ValueError("인증정보 확인 필요")
            old[code] = record
            changed += 1
        except Exception as exc:
            print(f"DART {code} 이전 자료 유지 ({type(exc).__name__})")
    if changed:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=DATA, delete=False) as tmp:
            json.dump(old, tmp, ensure_ascii=False, indent=2)
        Path(tmp.name).replace(OUTPUT)
    print(f"DART {len(due)}개 중 {changed}개 갱신")


def update_exits(snap=None, folder=None):
    """dart.json에는 있으나 지금 상장리츠 목록에 없는 종목의 최근 180일 공시에서 해산, 상장폐지 같은 보고서를 찾는다.
    목록에서 빠진 지 120일이 지난 종목(마지막 DART 갱신 기준)은 더 보지 않는다. 종목마다 주 1회만 조회한다."""
    key = None if folder else api_key()
    if folder or not key:
        return
    snap = snap or read(DATA / "latest.json")
    if not snap.get("listed"):
        return  # 상장 목록을 받지 못한 날은 빠진 종목을 판단하지 않는다
    records = read(OUTPUT) if OUTPUT.exists() else {}
    old = read(EXITS) if EXITS.exists() else {}
    today = date.today()
    current = {str(r["code"]) for r in snap["listed"]}
    gone = {code: r for code, r in records.items() if code not in current and r.get("corp_code")
            and (today - date.fromisoformat(r.get("fetched") or "2000-01-01")).days <= 120}
    out = {code: v for code, v in old.items() if code in gone}
    source = Source(None, key)
    for code in gone:
        if (today - date.fromisoformat(out.get(code, {}).get("fetched") or "2000-01-01")).days < 7:
            continue
        try:
            listing = source.request("list.json", {"corp_code": gone[code]["corp_code"], "page_count": 100,
                                                   "bgn_de": (today - timedelta(days=180)).strftime("%Y%m%d"),
                                                   "end_de": today.strftime("%Y%m%d")}, json.loads)
            if listing.get("status") not in ("000", "013"):
                raise DartError(listing.get("status"))
            filings = sorted(({"date": f"{x['rcept_dt'][:4]}-{x['rcept_dt'][4:6]}-{x['rcept_dt'][6:8]}",
                               "title": " ".join(x["report_nm"].split()), "rcept_no": x["rcept_no"]}
                              for x in listing.get("list", [])
                              if EXIT_TITLE.search(x.get("report_nm", "")) and re.fullmatch(r"\d{14}", x.get("rcept_no", ""))
                              and re.fullmatch(r"\d{8}", x.get("rcept_dt", ""))),
                             key=lambda x: x["rcept_no"], reverse=True)
            out[code] = {"name": gone[code].get("name"), "fetched": today.isoformat(), "filings": filings}
        except Exception as exc:
            print(f"DART 이탈 공시 {code} 이전 자료 유지 ({type(exc).__name__})")
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if key in text or re.search(r"[a-fA-F0-9]{40}", text):
        raise ValueError("인증정보 확인 필요")
    if out != old:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=DATA, delete=False) as tmp:
            tmp.write(text)
        Path(tmp.name).replace(EXITS)
    print(f"DART 상장 목록에서 빠진 종목 {len(gone)}개 공시 확인")


def money(value):
    return f"{value:,}원" if value is not None else "자료 없음"


def explain():
    for code, r in (read(OUTPUT) if OUTPUT.exists() else {}).items():
        print(f"\n{r['name']} ({code}) | {r['basis']} | {r['rcept_no']}")
        print("기간\t구간\t계정\t금액(원)")
        for key, label in (("current", "당기"), ("previous", "전기")):
            s = r[key]
            for a in s["accounts"]:
                group = "유동" if a["current"] else "비유동"
                print(label, group, a["name"], a["amount"] if a["amount"] is not None else "공란",
                      "" if a.get("included", True) else "합계에서 제외", sep="\t")
            print(f"{label} {s['start']} ~ {s['end']} | 유동 {s['short']:,}원 | 비유동 {s['long']:,}원 | 합계 {s['total']:,}원")
            if s.get("assets"):
                print(f"{label} 자산총계 {s['assets']:,}원 | 차입 ÷ 자산 {100 * s['total'] / s['assets']:.1f}%")
            print("금리와 이자 배수에 사용한 값", s.get("interest_source", s["cost_name"]) or "자료 없음",
                  "|", money(s.get("interest", s["cost"])))
            for a in s.get("interest_accounts", []):
                print(f"현금흐름표 {a['name']} ({a['account_id']}) | {money(a['amount'])}")
        print("전기 대비 차입 증감", money(r["current"]["total"] - r["previous"]["total"]))
        for x in r["dividends"]:
            print(f"{x['end']} | 주당 {money(x['dps'])} | {x['report']}")
        print("최근 12개월 주당 배당금", money(r["trailing_dps"]))
        for note in r["notes"]:
            print(note)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-dir", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--explain", action="store_true")
    parser.add_argument("--exits", action="store_true", help="상장 목록에서 빠진 종목의 해산, 상장폐지 공시만 확인")
    args = parser.parse_args()
    if args.explain:
        explain()
    elif args.exits:
        update_exits()
    else:
        update(folder=args.from_dir, force=args.force)

