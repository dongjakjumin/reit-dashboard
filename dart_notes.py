"""사업보고서 주석에서 당기 차입 건을 읽어 재무상태표와 대조한다."""
import argparse
import html
import io
import itertools
import json
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from dart import DATA, api_key, read

OUTPUT = DATA / "loans.json"
UNITS = {"원": 1, "천원": 1000, "백만원": 1000000, "억원": 10**8, "십억원": 10**9}
TABLE = re.compile(r"<TABLE\b[^>]*>.*?</TABLE>", re.I | re.S)
ROW = re.compile(r"<TR\b[^>]*>(.*?)</TR>", re.I | re.S)
CELL = re.compile(r"<(TD|TH|TE|TU)\b([^>]*)>(.*?)</\1>", re.I | re.S)
# 보유 금융상품 주석(단기금융상품의 전자단기사채 등). 전환사채의 파생금융상품은 빼고 본다
ASSET_NOTE = re.compile(r"(?<!파생)금융상품|금융자산|현금성자산")


def clean(raw):
    return " ".join(html.unescape(re.sub(r"</?[a-zA-Z][^>]*>", " ", raw)).replace("\u3000", " ").split())


def grid(raw):
    occupied = {}
    rows = []
    for i, tr in enumerate(ROW.findall(raw)):
        col = 0
        for tag, attrs, value in CELL.findall(tr):
            while (i, col) in occupied:
                col += 1
            spans = dict((k.lower(), int(v)) for k, v in re.findall(
                r'''(rowspan|colspan)\s*=\s*["']?(\d+)''', attrs, re.I))
            value = clean(value)
            for j in range(col, col + spans.get("colspan", 1)):
                for n in range(i, i + spans.get("rowspan", 1)):
                    occupied[n, j] = value
            col += spans.get("colspan", 1)
        width = max((j for n, j in occupied if n == i), default=-1) + 1
        rows.append([occupied[i, j] if (i, j) in occupied else "" for j in range(width)])
    return rows


def lead(context):
    """표 바로 앞 글: 마지막 완결 문장부터 끝까지(그 뒤의 소제목, 단위 표시 포함)."""
    end = context.rfind("다.")
    if end < 0:
        return context
    start = context.rfind("다.", 0, end)
    return context[start + 2 if start >= 0 else 0:]


def asset_note(context):
    """표 바로 앞 글이 보유 금융상품(단기금융상품의 전자단기사채 등)을 말하고, 차입이나 발행 사채는 말하지 않으면 True."""
    near = lead(context)
    # [review5] '회사채', '단기사채', '전자단기사채'처럼 앞에 다른 글자가 붙은 '사채'는 보유 상품 이름이라 발행 사채로 보지 않는다
    # 다만 '무보증사채', '공모사채', '전환사채'처럼 발행 사채의 종류를 앞에 붙인 말은 발행 사채로 본다
    return bool(ASSET_NOTE.search(near)) and not re.search(
        r"차입|발행|(?<![가-힣])사채|(?:무보증|보증|담보부|공모|사모|전환|신주인수권부?|교환)사채", near)


def candidates(xml, basis, body):
    for index, m in enumerate(TABLE.finditer(xml), 1):
        raw = m[0]
        context = clean(xml[max(0, m.start()-4500):m.start()])[-350:]
        # 표 바로 앞 글이 보유 금융상품(단기금융상품의 전자단기사채 등)을 말하면 차입 표로 보지 않는다
        if asset_note(context):
            continue
        # 사채 발행 내역 표(발행일, 만기일, 잔액)는 이자율 열이 없어도 후보로 둔다. 표 앞 글이 사채를 말할 때만이다
        # (칸 속 '전자단기사채'만으로는 차입 표로 보지 않는다)
        bond = re.search("발행일", raw) and re.search("만기일", raw) and re.search("사채", context)
        if not re.search("만기|상환일", raw) or not (re.search("이자율|금리|이율", raw) or bond):
            continue
        if re.search("차입|사채", clean(raw) + context):
            if body:
                titles = re.findall(r"<TITLE\b[^>]*>(.*?)</TITLE>", xml[:m.start()], re.S | re.I)
                title = clean(titles[-1]) if titles else ""
                section = next((clean(t) for t in reversed(titles) if "재무제표 주석" in clean(t)), "")
                if not re.match(r"\d+[.\-]", title) or "주석" not in section:
                    continue
                if ("연결" in section) != (basis == "CFS"):
                    continue
            tail = clean(xml[m.end():].split("<TABLE", 1)[0])[:6000]
            contexts = set(re.findall(r'ACONTEXT="([CP])FY', raw))
            if contexts:
                marker = {frozenset("C"): "당기", frozenset("P"): "전기"}.get(frozenset(contexts), "혼합")
                caption = period(context)
                context += " " + (marker if not caption or caption == marker else "혼합")
            yield index, context, grid(raw), tail


def filenames(r):
    no = r["rcept_no"]
    return [no + ("_00761.xml" if r["basis"] == "CFS" else "_00760.xml"), no + ".xml"]


DATE = re.compile(r"(?<!\d)(\d{4})\s*[./년-]\s*(\d{1,2})\s*[./월-]\s*(\d{1,2})(?!\d)")
ADJUST = re.compile(r"할인.*차금|전환권조정|상환할증금|Finance(?:Fee|Cost)", re.I)
VARIABLE = re.compile(r"변동|CD|KORIBOR|LIBOR|EURIBOR|SOFR|COFIX|MOR|기준금리\s*\+", re.I)
MARK = re.compile(r"\((?:주|\*)(\d*)\)")


FX = re.compile(r"외화|(?<![A-Za-z])(?:EUR|USD|JPY|GBP|AUD|CAD|CHF|HKD|SGD|CNY)(?![A-Za-z])"
                r"|유로|달러|엔화|(?<![가-힣])엔(?![가-힣])|파운드|위안|EURIBOR|SOFR|LIBOR|TONA|TIBOR|SONIA|€STR|ESTR|BBSW", re.I)


def number(s):
    s = s.replace(",", "").strip()
    if not re.fullmatch(r"-?\d+(?:\.\d+)?|\(\d+(?:\.\d+)?\)", s):
        return 0
    return Decimal(s.replace("(", "-").replace(")", ""))


def maturity(s):
    m = DATE.search(s)
    if m:
        try:
            return date(*map(int, m.groups())).isoformat()
        except ValueError:
            pass


def loan(kind, lender, day, text, flag, amount, fx, fraction=False):
    simple = MARK.sub("", text).strip()
    m = re.fullmatch(r"(?:연\s*)?(\d+(?:\.\d+)?)\s*%?", simple)
    category = "모름"
    for evidence in (flag, text):
        if "고정" in evidence:
            category = "고정"
            break
        if VARIABLE.search(evidence):
            category = "변동"
            break
    else:
        if m:
            category = "고정"
    rate = None
    if m and category == "고정":
        value = Decimal(m[1])
        rate = float(value * 100 if fraction and "%" not in simple and value < 1 else value)
    return dict(kind=kind, lender=lender if lender != "-" else "", maturity=day,
                rate_text=text, rate=rate, rate_type=category, amount=amount, fx=fx)


def ratio(rows):
    fixed = sum(x["amount"] for x in rows if x["rate_type"] == "고정")
    known = sum(x["amount"] for x in rows if x["rate_type"] != "모름")
    priced = [x for x in rows if x["rate"] is not None]
    weight = sum(x["amount"] for x in priced)
    return (fixed / known if known else None,
            sum(x["rate"] * x["amount"] for x in priced) / weight if weight else None)


def period(context):
    m = re.search(r"(당기|전기)(?:말)?[〉>)]?\s*(?:\(단위\s*:[^()]*\))?[.]?$", context)
    return m[1] if m else None


class Skip(ValueError):
    pass


def xbrl(rows, context, unit, foreign=False, fx_unit=False):
    if period(context) != "당기":
        raise Skip("전기 표" if period(context) else "XBRL 당기·전기 구분 불명")
    # 건을 열로 놓은 표와 행으로 놓은 표를 같은 방향으로 맞춘다.
    if any("명목금액" in s for s in rows[0]):
        rows = [list(col) for col in itertools.zip_longest(*rows, fillvalue="")]
    fields = {r[0].replace(" ", ""): r for r in rows if r and r[0]}
    names = [r for r in rows if r and not r[0]]
    amounts = next((v for k, v in fields.items() if "명목금액" in k and "외화" not in k
                    and (not foreign or "원화" in k or "KRW" in k) and "전기" not in k), [])
    principal = [r for k,r in fields.items() if k in ("단기차입금", "장기차입금")]
    if not amounts and not foreign and principal and any("총장부금액" in r for r in names):
        amounts = ["금액"] + [str(sum(number(r[j]) for r in principal)) for j in range(1,len(principal[0]))]
    if not amounts:
        raise Skip("원화 명목금액 없음")
    def field(pattern, j):
        return next((v[j] for k, v in fields.items() if re.fullmatch(pattern, k) and j < len(v)), "")
    loans = []
    adjustment = 0
    for j, value in enumerate(amounts[1:], 1):
        labels = [r[j] for r in names if j < len(r)]
        if "현재가치할인차금" in labels and principal:
            adjustment += int(number(value)*unit)
            continue
        if "거래상대방 합계" in labels or "장부금액 합계" in labels or any("명칭합계" in s.replace(" ", "") for s in labels):
            continue
        amount = int(number(value) * unit)
        if amount <= 0:
            continue
        day = maturity(field(r"(차입금,)?만기일?", j))
        if not day:
            raise Skip("명목금액에 대응하는 만기일 없음")
        kind = next((s for s in reversed(labels) if re.search("차입금|사채|매입", s)), "차입금")
        if principal:
            kind = next((r[0] for r in principal if number(r[j]) > 0), kind)
        lender = field("차입처", j) or (labels[-1] if "거래상대방" in labels else "")
        rate = field("(차입금,)?이자율", j)
        flag = ""
        # 이자율 유형별 보조 열은 같은 차입 건의 합계 열에 대응한다.
        if "이자율 유형 합계" in labels:
            for k in range(j-1, 0, -1):
                if names[-1][k] not in ("변동이자율", "고정이자율"):
                    break
                base = field("차입금,기준이자율", k)
                if base:
                    spread = field("차입금,기준이자율조정", k)
                    value = number(spread.rstrip("%").strip())
                    if "%" not in spread and abs(value) < 1:
                        value *= 100
                    rate, flag = f"{base} + {value:g}%", "변동"
                    break
                if field("차입금,이자율", k):
                    rate, flag = field("차입금,이자율", k), names[-1][k]
                    break
        fx = fx_unit or any(FX.search(k) for k in fields) or bool(FX.search(" ".join(
            [rate] + [r[j] for r in rows if j < len(r)])))
        loans.append(loan(kind, lender, day, rate, flag, amount, fx, fraction=True))
        adjustment += sum(int(number(v[j])*unit) for k,v in fields.items()
                          if ADJUST.search(k) and "외화" not in k and j < len(v))
    return loans, adjustment, 0


def parse_table(rows, context, tail, end=None):
    unit_tags = re.findall(r"단위\s*[:：]\s*([^()\n]*)", context + "\n" + rows[0][0])
    unit_text = unit_tags[-1] if unit_tags else ""
    won_pattern = r"십억원|억원|백만원|천원|(?<![가-힣])원(?![가-힣])"
    u = re.search(won_pattern, unit_text)
    foreign = bool(re.search(r"[A-Z]{3}|외화|달러|엔화|유로", unit_text))
    if not u and not foreign:
        raise Skip("원화 단위 없음")
    unit_name = u[0] if u else None
    if rows[0][0] == "" and any("명목금액" in s or s in ("단기차입금", "장기차입금")
                                    for row in rows for s in row):
        unit_name = unit_name or "원"
        loans, adj, transfer = xbrl(rows, context, UNITS[unit_name], foreign, bool(FX.search(unit_text)))
        return loans, adj, transfer, unit_name
    header_index = next((i for i,r in enumerate(rows[:4]) if any(re.search("만기|상환일", s) for s in r)
                         and any(re.search("이자율|금리|이율", s) for s in r)), None)
    if header_index is None:
        # 이자율 열 없이 발행일과 만기일을 한 줄에 적은 사채 표. 이자율은 '모름'으로 둔다(고정 비중과 평균 금리에서 빠짐)
        header_index = next((i for i, r in enumerate(rows[:4]) if any("발행일" in s for s in r)
                             and any("만기일" in s for s in r)), None)
        if header_index is None or not re.search("사채", context) or asset_note(context):
            raise Skip("만기일과 이자율을 함께 기재한 잔액 표 없음")
    h = [s.replace(" ", "") for s in rows[header_index]]
    body = rows[header_index+1:]
    sub = []
    if body and any(
            re.search("^(당기|전기|원화|외화|이자율|금액|잔액)", v.replace(" ", "")) or (end and maturity(v) == end)
            for v in body[0]) and not any(number(v) for v in body[0]):
        sub, body = [s.replace(" ", "") for s in body[0]], body[1:]
    def column(pattern):
        return next((i for i,s in enumerate(h) if re.search(pattern,s)), None)
    # 당기말 아래가 이자율과 금액으로 나뉜 표는 금액 칸만 잔액 열로 본다
    rate_sub = lambda i: i < len(sub) and re.search("이자율|금리|이율", sub[i])
    cur = next((i for i,s in enumerate(h) if (s == "당기말" and not rate_sub(i)) or
                (s == "금액" and (sub[i] == "당기말" if i < len(sub) else period(context) == "당기"))), None)
    if foreign or any(re.match(r"외화(?:금액|환산|잔액|표시|$)", s) for s in h + sub) or any(
            re.fullmatch("USD|EUR|JPY|GBP", s) for r in body for s in r):
        cur = None
        for i, heading in enumerate(h):
            label = heading + (sub[i] if i < len(sub) else "")
            if "원화" not in label and "KRW" not in label:
                continue
            label_day = maturity(label)
            if "전기" in label or (label_day and end and label_day != end):
                continue
            if not ("당기" in label or (end and label_day == end)):
                continue
            col_unit = re.search(won_pattern, label)
            unit_name = col_unit[0] if col_unit else unit_name or "원"
            cur = i
            break
        if cur is None:
            raise Skip("외화 표에 당기 원화 환산액 없음")
    unit = UNITS[unit_name]
    if cur is None:
        raise Skip("당기말 잔액 열 없음")
    # 고른 열 값이 모두 100 이하이면 잔액이 아니라 이자율을 고른 것으로 본다(억원 이상 단위 표는 작은 값이 정상일 수 있어 뺀다)
    picked = [number(r[cur]) for r in body if cur < len(r) and number(r[cur])]
    if unit < UNITS["억원"] and picked and max(picked) <= 100:
        raise Skip("잔액 열 값이 이자율 크기")
    day_col = column("만기|상환일")
    rate_col = column("이자율|이율|연금리|^금리(?:[(%]|$)")
    lender_col = column("차입처|금융기관|인수기관|거래은행")
    kind_col = column("구분|유형|종류")
    flag_col = column("변동|고정|금리구분")
    kinds = re.findall("유동성장기차입금|단기차입금|장기차입금|전환사채|차입금|사채",context)
    default = kinds[-1] if kinds else "차입금"
    loans, adj, transfer = [], 0, 0
    prior = None
    refs = {m[1]:m[2] for m in re.finditer(r"\((?:주|\*)(\d*)\)\s*(.*?)(?=\((?:주|\*)\d*\)|$)", tail,re.S)}
    fx_header = bool(FX.search(" ".join([unit_text, *h, *sub])))
    for row in body:
        if prior and len(row) in (2,3) and len(row)<len(h) and h[-2:]==["당기말","전기말"] and all(
                re.fullmatch(r"[\d,()-]+", s) for s in row[-2:]):
            full = prior.copy()
            full[-2:] = row[-2:]
            if lender_col is not None:
                full[lender_col] = row[0] if len(row)==3 else ""
            row = full
        def cell(i):
            return row[i] if i is not None and i < len(row) else ""
        value = int(number(cell(cur))*unit)
        labels = " ".join(s.replace(" ","") for s in row)
        if re.search("합계|소계|총계|차감계|대체후|장부금액|잔액", labels):
            prior = None
            continue
        if ADJUST.search(labels):
            adj += value
        elif "대체" in labels or re.search("차감.*유동성", labels):
            transfer += value
        else:
            day = maturity(cell(day_col))
            prior = row if day else None
            if value <= 0:
                continue
            if not day:
                raise Skip("당기 잔액의 만기일 판독 불가")
            text = cell(rate_col)
            flag = cell(flag_col)
            if not flag:
                for ref in MARK.findall(text + cell(kind_col)):
                    note = refs.get(ref,"")
                    if re.search("변동금리|고정금리",note):
                        flag = re.search("변동금리|고정금리",note)[0]
                        break
            fx = fx_header or bool(FX.search(" ".join(row)))
            loans.append(loan(cell(kind_col) or default, cell(lender_col), day, text, flag, value, fx))
    return loans, adj, transfer, unit_name


def status(total, nominal, adjusted):
    if not total:
        return "차입 없음"
    if abs(adjusted-total)*100 <= total:
        return "일치"
    if abs(nominal-total)*100 <= 3*total:
        return "근사"
    return "불일치"


def extract(r, doc, xml):
    total, end = r["current"]["total"], r["current"]["end"]
    tables, skipped = [], []
    ambiguous = False
    for i, context, rows, tail in candidates(xml, r["basis"], doc == filenames(r)[1]):
        if not total or not rows or not rows[0]:
            continue
        try:
            loans, adj, transfer, unit = parse_table(rows, context, tail, end)
        except Skip as exc:
            skipped.append(f"표 {i}: {exc}")
            ambiguous |= str(exc) == "XBRL 당기·전기 구분 불명"
            continue
        if loans:
            tables.append((i, loans, adj, transfer, unit, sum(x["amount"] for x in loans)))
        else:
            skipped.append(f"표 {i}: 당기 잔액 없음")
    # 같은 잔액을 여러 표에서 설명할 수 있다. 표 전체만 선택한다.
    best = ()
    def score(group):
        nominal = sum(t[5] for t in group)
        net = nominal + sum(t[2] for t in group)
        return ({"일치":0,"근사":1,"불일치":2,"차입 없음":0}[status(total,nominal,net)],
                abs(net-total), len(group))
    for n in range(1, len(tables)+1):
        for group in itertools.combinations(tables,n):
            if not best or score(group) < score(best):
                best = group
    seen = set()
    for index, rows, *_ in best:
        keys = {(x["lender"], x["maturity"], x["rate_text"], x["amount"]) for x in rows}
        if seen.intersection(keys):
            ambiguous = True
            skipped.append(f"표 {index}: 다른 표와 차입처, 만기일, 이자율 원문, 금액 중복")
        seen.update(keys)
    chosen = [t[0] for t in best]
    skipped.extend(f"표 {t[0]}: 다른 표 조합을 채택" for t in tables if t[0] not in chosen)
    if not tables and total and not skipped:
        skipped.append("만기일과 이자율이 함께 있는 차입 잔액 표 없음")
    loans = [x for t in best for x in t[1]]
    nominal, adj = sum(t[5] for t in best), sum(t[2] for t in best)
    share, avg = ratio(loans)
    return dict(name=r["name"], rcept_no=r["rcept_no"], basis=r["basis"], doc=doc,
                unit=", ".join(sorted({t[4] for t in best})) or None, end=end, tables=chosen,
                loans=loans, nominal=nominal, adjustments=adj, reclassifications=sum(t[3] for t in best),
                bs_total=total, diff=nominal+adj-total,
                match="불일치" if ambiguous else status(total,nominal,nominal+adj),
                fixed_share=share, avg_rate=avg, skipped=skipped,
                warn=[f"{x['kind']} {x['lender']} 만기일({x['maturity']})이 기준일보다 앞섭니다."
                      for x in loans if x["maturity"] < end])


def document(code, r, folder, key):
    names = filenames(r)
    if folder:
        for name in names:
            path = Path(folder)/code/name
            if path.exists():
                return name, path.read_text(encoding="utf-8-sig")
        raise FileNotFoundError
    query = urllib.parse.urlencode(dict(crtfc_key=key, rcept_no=r["rcept_no"]))
    with urllib.request.urlopen("https://opendart.fss.or.kr/api/document.xml?"+query, timeout=45) as response:
        archive = io.BytesIO(response.read())
    with zipfile.ZipFile(archive) as z:
        for name in names:
            if name in z.namelist():
                return name, z.read(name).decode("utf-8-sig")
    raise FileNotFoundError


def update(snap=None, folder=None, force=False):
    source = read(DATA/"dart.json")
    stored = read(OUTPUT) if OUTPUT.exists() else {}
    due = []
    for code, record in source.items():
        note = stored.get(code, {})
        expected = (record["rcept_no"], record["basis"], record["current"]["end"], record["current"]["total"])
        if folder or force or tuple(note.get(k) for k in ("rcept_no", "basis", "end", "bs_total")) != expected:
            due.append(code)
    if not due:
        return
    key = None if folder else api_key()
    if not folder and not key:
        print("DART 인증키가 없어 주석 수집을 건너뜁니다.")
        return
    changed = 0
    for n, code in enumerate(due):
        try:
            if n and not folder:
                time.sleep(1)
            r = source[code]
            result = extract(r, *document(code,r,folder,key))
            payload = json.dumps(result, ensure_ascii=False)
            if (key and key in payload) or re.search(r"[0-9a-fA-F]{40}",payload):
                raise ValueError
            stored[code] = result
            changed += 1
        except Exception as exc:
            print(code, type(exc).__name__)
    if changed:
        with tempfile.NamedTemporaryFile(dir=DATA.parent, mode="w", encoding="utf8", delete=False) as f:
            json.dump(stored, f, ensure_ascii=False, indent=2)
        Path(f.name).replace(OUTPUT)
    print(f"DART 주석 {len(due)}개 종목 중 {changed}개 종목 갱신")


def explain():
    for code,r in read(OUTPUT).items():
        print(f"{code} {r['name']} | {r['doc']} | 표 {r['tables']} | {len(r['loans'])}건 | "
              f"명목 {r['nominal']:,}원 | 조정 {r['adjustments']:,}원 | 재무상태표 {r['bs_total']:,}원 | "
              f"차이 {r['diff']:,}원 | {r['match']}")
        for line in r["skipped"] + r["warn"]:
            print("  " + line)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-dir", type=Path)
    parser.add_argument("--explain", action="store_true")
    args = parser.parse_args()
    if args.explain:
        explain()
    else:
        update(folder=args.from_dir)

