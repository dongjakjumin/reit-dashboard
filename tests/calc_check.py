"""사업성 계산 엔진 검산.

1) 손으로 답을 알 수 있는 경우(금리 0, 전액 자기자본, 대출 한도 등)를 계산해 맞는지 본다.
2) 경계에 가까운 가정(보유 30년, 개발기간 20년, 전액 자기자본, 낮은 임대료)에서 값이 말이 되는지 본다.
3) 기본 예시와 무작위 가정 400가지를 자바스크립트 엔진(calc_engine.js)과 파이썬 참조 구현(calc_ref.py)에
   똑같이 넣고 결과가 같은지 대조한다.

node가 필요하다. 하나라도 어긋나면 종료 코드 1.
"""
from __future__ import annotations

import json
import math
import random
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import calc_ref  # noqa: E402

ROOT = Path(__file__).parent.parent
BASE = json.loads((ROOT / "calc_defaults.json").read_text(encoding="utf-8"))["base"]

RUNNER = r"""
const { reitCalc } = require(process.argv[1]);
let buf = '';
process.stdin.on('data', d => buf += d);
process.stdin.on('end', () => {
  const out = JSON.parse(buf).map(p => {
    const r = reitCalc(p);
    return {
      acq: r.acq, hard: r.hard, soft: r.soft, fin: r.fin, tpc: r.tpc, equity: r.equity, dev_debt: r.dev_debt,
      dev_debt_peak: r.dev_debt_peak, cash_at_stab: r.cash_at_stab,
      ltc: r.ltc, debt_to_equity: r.debt_to_equity, Tc: r.Tc, Ts: r.Ts, Tend: r.Tend,
      lease_interest: r.lease_interest, lease_noi: r.lease_noi, debt_at_stab: r.debt_at_stab,
      noi1: r.noi1, value: r.value, yield_on_cost: r.yield_on_cost, dev_margin: r.dev_margin,
      'a.sale': r.a.sale, 'a.net': r.a.net, 'a.irr': r.a.irr, 'a.multiple': r.a.multiple, 'a.profit': r.a.profit,
      'b.loan': r.b.loan, 'b.refi': r.b.refi, 'b.loan_to_equity': r.b.loan_to_equity, 'b.sale': r.b.sale, 'b.constraint': r.b.constraint, 'b.paid_in': r.b.paid_in,
      'b.years.yield_on_equity': r.b.years.map(x => x.yield_on_equity),
      'b.net': r.b.net, 'b.irr': r.b.irr, 'b.multiple': r.b.multiple, 'b.profit': r.b.profit, 'b.dscr_min': r.b.dscr_min,
      'b.years.noi': r.b.years.map(x => x.noi), 'b.years.cf': r.b.years.map(x => x.cf),
    };
  });
  process.stdout.write(JSON.stringify(out));
});
"""


def run_js(cases: list[dict]) -> list[dict]:
    res = subprocess.run(["node", "-e", RUNNER, str(ROOT / "calc_engine.js")],
                         input=json.dumps(cases), capture_output=True, text=True, encoding="utf-8")
    if res.returncode != 0:
        raise RuntimeError(res.stderr[:500])
    return json.loads(res.stdout)


def same(a, b, tol=1e-7) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(b, str):
        return a == b
    if isinstance(a, list):
        return len(a) == len(b) and all(same(x, y, tol) for x, y in zip(a, b))
    return math.isclose(a, b, rel_tol=tol, abs_tol=1e-7)


def random_case(rng: random.Random) -> dict:
    return {
        "land": rng.uniform(100, 5000), "acq_pct": rng.uniform(0, 6),
        "gfa_py": rng.uniform(2000, 40000), "cost_per_py": rng.uniform(400, 1600), "soft_pct": rng.uniform(5, 25),
        "pre_m": rng.randint(0, 36), "con_m": rng.randint(6, 72), "lease_m": rng.randint(0, 36),
        "hold_y": rng.randint(1, 30), "equity_pct": rng.uniform(5, 100),
        "dev_rate": rng.uniform(0, 15), "rent": rng.uniform(3, 40), "vacancy": rng.uniform(0, 20),
        "opex": rng.uniform(5, 35), "growth": rng.uniform(-2, 4), "reit_cost": rng.uniform(0, 0.8),
        "perm_dscr_min": rng.choice([0, rng.uniform(0.5, 4)]), "rent_area_pct": rng.uniform(0, 100),
        "perm_ltv": rng.uniform(0, 80), "perm_rate": rng.uniform(0, 12),
        "exit_cap": rng.uniform(3.5, 8), "sale_cost": rng.uniform(0, 3),
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    bad = checks = 0

    def check(ok: bool, msg: str):
        nonlocal bad, checks
        checks += 1
        print(("  통과  " if ok else "  실패  ") + msg)
        bad += 0 if ok else 1

    print("[1] 손으로 답을 아는 경우")
    # (가) 금리 0, 전액 자기자본, 안정화 기간 0: 총사업비 = 사업비 합계, A안 회수 = 매각가 × (1 - 매각비용)
    p = dict(BASE, dev_rate=0, equity_pct=100, lease_m=0, reit_cost=0, acq_pct=0, soft_pct=0,
             land=1000, gfa_py=10000, cost_per_py=1000, rent=10, vacancy=0, opex=0, exit_cap=5, sale_cost=0)
    r = run_js([p])[0]
    check(same(r["tpc"], 2000) and same(r["equity"], 2000) and same(r["dev_debt"], 0), "금리 0, 전액 자기자본: 총사업비 2,000억 = 자기자본, 차입 0")
    check(same(r["noi1"], 120) and same(r["value"], 2400) and same(r["a.net"], 2400), "NOI 120억, 가치 2,400억, A안 회수 2,400억")
    check(same(r["a.multiple"], 1.2) and same(r["dev_margin"], 0.2), "A안 회수 배수 1.2배, 개발이익률 20%")
    # (나) 금리 0, 자기자본 30%: 차입 = 총사업비의 70%, 금융비 0
    p2 = dict(p, equity_pct=30)
    r2 = run_js([p2])[0]
    check(same(r2["dev_debt"], 1400) and same(r2["fin"], 0) and same(r2["ltc"], 0.7), "금리 0, 자기자본 30%: 차입 1,400억, 금융비 0, LTC 70%")
    check(same(r2["a.net"], 1000) and same(r2["a.profit"], 400), "A안 회수 1,000억(2,400 - 1,400), 이익 400억")
    # (다) B안: LTV 50%, 금리 4%, 성장 0, 5년 보유 → 대출 1,200억, 연 이자 48억, 연 현금흐름 72억, DSCR 2.5
    p3 = dict(p2, perm_ltv=50, perm_rate=4, growth=0, hold_y=5)
    r3 = run_js([p3])[0]
    check(same(r3["b.loan"], 1200) and same(r3["b.refi"], -200), "B안 담보대출 1,200억, 전환 시 추가 출자 200억")
    check(all(same(x, 72) for x in r3["b.years.cf"]) and same(r3["b.dscr_min"], 2.5), "연 현금흐름 72억(120 - 48), DSCR 2.5배")
    check(same(r3["b.net"], 1200) and same(r3["b.profit"], -600 - 200 + 72 * 5 + 1200), "매각 회수 1,200억, 누적 이익 760억")
    # (라) 한 번 투자하고 12개월 뒤 10% 불려 받으면 IRR 10%
    p4 = dict(p, land=1000, gfa_py=10000, cost_per_py=0, pre_m=1, con_m=11, exit_cap=5, rent=55 / 12)  # NOI 55억, 가치 1,100억
    r4 = run_js([p4])[0]
    check(same(r4["value"], 1100, 1e-6) and same(r4["a.irr"], (1.1) ** (12 / 11) - 1, 1e-6),
          "1,000억을 1개월째 넣고 12개월째 1,100억 회수: 월 기준 11개월 → 연 IRR 일치")

    flows = {1: -100, 2: 300}
    js_irr = subprocess.check_output(["node", "-e",
        "console.log(require(process.argv[1]).irrMonthly([0,-100,300]))", str(ROOT / "calc_engine.js")])
    check(same(float(js_irr), 2) and same(calc_ref.month_irr(flows), 2), "한 달 뒤 100억에서 300억 회수 시 월 IRR 200%")
    check(same(r3["b.paid_in"], 800) and same(r3["b.loan_to_equity"], 1.5)
          and same(r3["b.years.yield_on_equity"][0], 0.09), "추가 출자 후 자기자본 800억, 차입 1.5배, 현금수익률 9%")
    limited, disabled, free, half, empty = run_js([
        dict(p3, perm_dscr_min=3, reit_cost=1.5), dict(p3, perm_dscr_min=0, reit_cost=1.5),
        dict(p3, perm_rate=0, equity_pct=100), dict(p3, rent_area_pct=50), dict(p3, rent_area_pct=0)])
    check(same(limited["b.loan"], 750) and limited["b.constraint"] == "dscr"
          and same(limited["b.dscr_min"], 3), "운영비 30억 차감 후 DSCR 3배 기준 대출 한도 750억")
    check(same(disabled["b.loan"], 1200) and disabled["b.constraint"] == "ltv", "최소 DSCR 0이면 LTV 적용")
    check(same(free["b.paid_in"], 2000) and free["b.refi"] > 0 and free["b.dscr_min"] is None
          and free["b.constraint"] == "ltv", "무이자 대출 회수 시 처음 자기자본 유지")
    check(same(half["hard"], 1000) and same(half["noi1"], 60), "임대면적 50%에서 NOI 60억, 공사비 1,000억")
    check(same(empty["b.loan"], 0) and same(empty["noi1"], 0), "임대면적 0에서 대출과 NOI 모두 0")
    print("[2] 경계에 가까운 가정")
    # (마) 보유 30년에 대출 금리가 높아 수익이 거의 없는 경우: IRR이 터무니없는 값이 되지 않아야 한다
    long_case = dict(BASE, perm_rate=9, perm_dscr_min=0, growth=0, hold_y=30)
    r5 = run_js([long_case])[0]
    ref5 = calc_ref.calc(long_case)
    check(r5["b.irr"] is not None and -0.05 < r5["b.irr"] < 0.05 and same(r5["b.irr"], ref5["b.irr"]),
          f"보유 30년, 대출 금리 9%, 최소 DSCR 미적용, 임대료 상승 0%: B안 IRR {r5['b.irr'] * 100:.3f}% (참조 구현과 일치)")
    # (바) 개발기간이 아주 길어도 자기자본 = 총사업비 × 비율이 맞아야 한다
    slow = dict(BASE, pre_m=120, con_m=120, equity_pct=40)
    r6 = run_js([slow])[0]
    check(same(r6["equity"] / r6["tpc"], 0.40, 1e-9), f"개발기간 20년, 자기자본 비율 40%: 자기자본 ÷ 총사업비 = {r6['equity'] / r6['tpc']:.6f}")
    steep = dict(BASE, pre_m=120, con_m=120, equity_pct=50, dev_rate=30)
    r7 = run_js([steep])[0]
    check(same(r7["equity"] / r7["tpc"], 0.50, 1e-9), f"같은 기간에 금리 30%, 비율 50%: 자기자본 ÷ 총사업비 = {r7['equity'] / r7['tpc']:.6f}")
    # (사) 전액 자기자본이면 차입 잔액은 음수가 될 수 없고, 안정화 기간에 남은 돈은 따로 쌓인다
    r8 = run_js([dict(BASE, equity_pct=100)])[0]
    check(same(r8["dev_debt"], 0) and same(r8["debt_at_stab"], 0) and r8["cash_at_stab"] > 0,
          f"전액 자기자본: 차입 0, 안정화 기간에 쌓인 돈 {r8['cash_at_stab']:.1f}억")
    # (아) 임대 순수익이 이자보다 작으면 안정화 기간에 차입이 늘고, 최고 잔액은 안정화 시점 잔액이다
    r9 = run_js([dict(BASE, rent=5)])[0]
    check(r9["debt_at_stab"] > r9["dev_debt"] and same(r9["dev_debt_peak"], r9["debt_at_stab"]),
          "임대료가 낮은 경우: 안정화 기간에 차입 증가, 최고 잔액 = 안정화 시점 잔액")

    print("[3] 자바스크립트 엔진과 파이썬 참조 구현 대조")
    rng = random.Random(20261122)
    cases = [BASE] + [random_case(rng) for _ in range(400)]
    js = run_js(cases)
    mismatch = []
    for i, (p, j) in enumerate(zip(cases, js)):
        ref = calc_ref.calc(p)
        for k, v in ref.items():
            if not same(j.get(k), v):
                mismatch.append((i, k, j.get(k), v))
    check(not mismatch, f"기본 예시 + 무작위 가정 400가지(보유 30년, 개발기간 9년까지), 항목 {len(calc_ref.calc(BASE))}개씩 대조"
          + (f" (불일치 {len(mismatch)}건, 첫 건 {mismatch[0]})" if mismatch else ""))
    b = js[0]
    print(f"  기본 예시: 총사업비 {b['tpc']:,.0f}억, 자기자본 {b['equity']:,.0f}억, 안정화 NOI {b['noi1']:,.1f}억, "
          f"가치 {b['value']:,.0f}억, A안 IRR {b['a.irr']*100:.2f}%, B안 IRR {b['b.irr']*100:.2f}%")
    expected = [5019, 1506, 288, 6130, 10.54, 9.36]
    actual = [round(b[k]) for k in ("tpc", "equity", "noi1", "value")]
    actual += [round(b[k] * 100, 2) for k in ("a.irr", "b.irr")]
    check(actual == expected, "기본 가정의 금액과 IRR 6개 유지")
    print(f"결과: {checks}개 중 {checks - bad}개 통과")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
