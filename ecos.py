"""한국은행 ECOS의 일별 시장금리를 계열별로 갱신합니다."""
import argparse
import json
import os
import sys
import tempfile
import time
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "data" / "rates.json"
SERIES = {
    "ktb3": ("국고채(3년)", "817Y002", "010200000"),
    "ktb5": ("국고채(5년)", "817Y002", "010200001"),
    "ktb10": ("국고채(10년)", "817Y002", "010210000"),
    "corp_aa": ("회사채(3년, AA-)", "817Y002", "010300000"),
    "cd91": ("CD(91일)", "817Y002", "010502000"),
    "base": ("한국은행 기준금리", "722Y001", "0101000"),
}


def api_key():
    key = os.environ.get("ECOS_API_KEY", "").strip()
    if not key and (ROOT / ".env").is_file():
        for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("ECOS_API_KEY="):
                key = line.partition("=")[2].strip().strip("\"'")
                break
    return key or "sample"


def fetch(stat, item, start, end, key, folder=None):
    size = 10 if key == "sample" else 1000
    first, total = 1, None
    values = {}
    while total is None or first <= total:
        if folder is not None:
            page = Path(folder) / f"{stat}_{item}_{first}.json"
            payload = json.loads(page.read_text(encoding="utf-8-sig"))
        else:
            url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{quote(key, safe='')}/json/kr/"
                   f"{first}/{first + size - 1}/{stat}/D/{start:%Y%m%d}/{end:%Y%m%d}/{item}")
            time.sleep(0.5)
            with urlopen(url, timeout=20) as response:
                payload = json.load(response)
        if "RESULT" in payload:
            if first == 1 and payload["RESULT"]["CODE"] == "INFO-200":
                return {}
            raise ValueError("ECOS 응답 오류")
        page = payload["StatisticSearch"]
        count = int(page["list_total_count"])
        rows = page["row"]
        if (total is not None and total != count) or len(rows) != min(size, count - first + 1):
            raise ValueError("ECOS 행 수 오류")
        total = count
        for row in rows:
            day = datetime.strptime(row["TIME"], "%Y%m%d").date()
            value = float(row["DATA_VALUE"])
            if (not 0 < value < 20 or day.isoformat() in values
                    or row.get("STAT_CODE", stat) != stat or row.get("ITEM_CODE1", item) != item):
                raise ValueError("ECOS 관측값 오류")
            if start <= day <= end:
                values[day.isoformat()] = value
        first += size
    return values


def update(snap=None, folder=None, force=False):
    today = (date(2026, 11, 22) if folder is not None else
             date.fromisoformat(snap["date"]) if snap else date.today())
    old = json.loads(OUTPUT.read_text(encoding="utf-8")) if OUTPUT.exists() else {}
    if not force and old.get("fetched") == today.isoformat():
        return
    key = "sample" if folder is not None else api_key()
    series = old.get("series", {})
    changed = 0
    for code, (name, stat, item) in SERIES.items():
        try:
            previous = series.get(code, {}).get("values", {})
            if previous:
                start = date.fromisoformat(max(previous)) - timedelta(days=7)
            elif key == "sample":
                start = today - timedelta(days=30)
            else:
                year = today.year - 3
                start = date(year, today.month, min(today.day, monthrange(year, today.month)[1]))
            fresh = fetch(stat, item, start, today, key, folder)
            values = dict(sorted((previous | fresh).items()))
            series[code] = dict(name=name, stat=stat, item=item, unit="연%", values=values)
            changed += 1
        except Exception as exc:
            print(f"{name} 이전 자료 유지 ({type(exc).__name__})")
    if changed:
        record = dict(source="한국은행 ECOS", fetched=today.isoformat(), series=series)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=OUTPUT.parent, delete=False) as tmp:
            json.dump(record, tmp, ensure_ascii=False, indent=2)
        Path(tmp.name).replace(OUTPUT)
    print(f"금리 {len(SERIES)}개 계열 중 {changed}개 갱신")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-dir", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    update(folder=args.from_dir, force=args.force)
