"""사업성 계산 엔진 검산.

1) 손으로 답을 알 수 있는 경우 4가지(금리 0, 전액 자기자본 등)를 계산해 맞는지 본다.
2) 기본 예시와 무작위 가정 200가지를 자바스크립트 엔진(calc_engine.js)과 파이썬 참조 구현(calc_ref.py)에
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
      ltc: r.ltc, debt_to_equity: r.debt_to_equity, Tc: r.Tc, Ts: r.Ts, Tend: r.Tend,
      lease_interest: r.lease_interest, lease_noi: r.lease_noi, debt_at_stab: r.debt_at_stab,
      noi1: r.noi1, value: r.value, yield_on_cost: r.yield_on_cost, dev_margin: r.dev_margin,
      'a.sale': r.a.sale, 'a.net': r.a.net, 'a.irr': r.a.irr, 'a.multiple': r.a.multiple, 'a.profit': r.a.profit,
      'b.loan': r.b.loan, 'b.refi': r.b.refi, 'b.loan_to_equity': r.b.loan_to_equity, 'b.sale': r.b.sale,
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
    if isinstance(a, list):
        return len(a) == len(b) and all(same(x, y, tol) for x, y in zip(a, b))
    return math.isclose(a, b, rel_tol=tol, abs_tol=1e-7)


def random_case(rng: random.Random) -> dict:
    return {
        "land": rng.uniform(100, 5000), "acq_pct": rng.uniform(0, 6),
        "gfa_py": rng.uniform(2000, 40000), "cost_per_py": rng.uniform(400, 1600), "soft_pct": rng.uniform(5, 25),
        "pre_m": rng.randint(1, 24), "con_m": rng.randint(12, 48), "lease_m": rng.randint(0, 24),
        "hold_y": rng.randint(1, 10), "equity_pct": rng.uniform(10, 100),
        "dev_rate": rng.uniform(0, 12), "rent": rng.uniform(3, 40), "vacancy": rng.uniform(0, 20),
        "opex": rng.uniform(5, 35), "growth": rng.uniform(-1, 4), "reit_cost": rng.uniform(0, 0.8),
        "perm_ltv": rng.uniform(0, 70), "perm_rate": rng.uniform(0, 8),
        "exit_cap": rng.uniform(3.5, 8), "sale_cost": rng.uniform(0, 3),
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    bad = 0

    def check(ok: bool, msg: str):
        nonlocal bad
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

    print("[2] 자바스크립트 엔진과 파이썬 참조 구현 대조")
    rng = random.Random(20261122)
    cases = [BASE] + [random_case(rng) for _ in range(200)]
    js = run_js(cases)
    mismatch = []
    for i, (p, j) in enumerate(zip(cases, js)):
        ref = calc_ref.calc(p)
        for k, v in ref.items():
            if not same(j.get(k), v):
                mismatch.append((i, k, j.get(k), v))
    check(not mismatch, f"기본 예시 + 무작위 가정 200가지, 항목 {len(calc_ref.calc(BASE))}개씩 대조"
          + (f" (불일치 {len(mismatch)}건, 첫 건 {mismatch[0]})" if mismatch else ""))
    b = js[0]
    print(f"  기본 예시: 총사업비 {b['tpc']:,.0f}억, 자기자본 {b['equity']:,.0f}억, 안정화 NOI {b['noi1']:,.1f}억, "
          f"가치 {b['value']:,.0f}억, A안 IRR {b['a.irr']*100:.2f}%, B안 IRR {b['b.irr']*100:.2f}%")
    print("결과:", "모두 통과" if bad == 0 else f"실패 {bad}건")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
