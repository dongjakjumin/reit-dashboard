"""사업성 계산의 파이썬 참조 구현.

calc_engine.js와 같은 계산 규칙을 따로 구현한 것이다. 화면이 쓰는 자바스크립트 엔진의 결과를
이 구현의 결과와 대조해(calc_check.py) 계산 오류를 찾는다. 코드는 서로 베끼지 않고 규칙만 공유한다.

규칙(금액 억원, 기간 개월)
1. 공사비 = 연면적(평) × 평당 공사비(만원). 기타 사업비 = 공사비 × 비율. 취득 부대비용과 매입보수 = 토지비 × 각 비율.
2. 토지비, 부대비용, 매입보수는 첫 달, 기타 사업비는 준공까지 매달 균등, 공사비는 공사기간에 매달 균등하게 나간다.
3. 매달 필요한 돈(사업비 + 차입 이자)은 자기자본에서 먼저 쓰고, 다 쓰면 차입한다. 이자는 원금에 더한다.
4. 총사업비 = 사업비 합계 + 준공까지의 이자. 자기자본 = 총사업비 × 자기자본 비율이 되는 값을 찾는다.
5. 임대 안정화 기간에는 입주율이 매달 같은 폭으로 오른다. 임대 순수익으로 이자, 리츠 운영비용, 운용보수
   (둘 다 총사업비 × 연 비율 ÷ 12)를 내고, 모자라면 차입이 늘고 남으면 차입을 갚는다.
   차입이 0이 된 뒤 남는 돈은 이자 없이 모아 안정화 시점에 받는다.
6. 안정화 가치 = 안정화 첫해 NOI ÷ Cap Rate.
7. A안: 안정화 시점에 매각. 회수 = 매각가 × (1 - 매각비용 - 매각보수) - 차입 잔액 + 모아 둔 돈.
8. B안: 안정화 시점에 LTV와 이자 기준 DSCR로 담보대출 한도를 정해 개발 차입을 갚고,
   해마다 NOI - 이자 - 리츠 운영비용 - 운용보수를 받는다. DSCR의 분자도 NOI - 리츠 운영비용 - 운용보수다.
   보유 기간 끝에 다음 해 NOI ÷ Cap Rate로 매각하고 매각비용, 매각보수, 대출을 뺀다.
9. IRR은 월별 자기자본 현금흐름의 월 내부수익률을 연율로 바꾼 값이다.
10. 회수 배수 = 받은 돈 합계 ÷ 넣은 돈 합계. 같은 달에 넣고 받은 돈도 상계하지 않는다.
11. 개발 단계 차입 배수 = (준공부터 안정화까지의 최고 차입 잔액) ÷ 자기자본.
12. 자산관리회사 보수 합계 = 매입보수 + 준공 뒤 운용보수(A안은 안정화까지, B안은 운영 기간 끝까지) + 매각보수.
"""
from __future__ import annotations

import math


def month_irr(flows: dict[int, float]) -> float | None:
    """월 내부수익률. 현재가치 또는 미래가치의 부호가 바뀌는 지점을 찾는다."""
    if not any(v < 0 for v in flows.values()) or not any(v > 0 for v in flows.values()):
        return None
    events = sorted((t, v) for t, v in flows.items() if v)
    first, last = events[0][0], events[-1][0]
    single = sum((v > 0) != (w > 0) for (_, v), (_, w) in zip(events, events[1:])) == 1

    def f(r: float) -> float:
        origin = first if r > 0 else last
        return math.fsum(v * (1.0 + r) ** (origin - t) for t, v in events)

    a, b = -1.0 if single else -0.9, 1.0
    fa, fb = f(a), f(b)
    while fa * fb > 0:
        b *= 2
        if not single or not math.isfinite(b):
            return None
        fb = f(b)
    for _ in range(200):
        c = (a + b) / 2.0
        if c == a or c == b:
            break
        fc = f(c)
        if fa * fc <= 0:
            b, fb = c, fc
        else:
            a, fa = c, fc
    return (a + b) / 2.0


def yearly(r: float | None) -> float | None:
    if r is None:
        return None
    try:
        v = (1.0 + r) ** 12 - 1.0
    except OverflowError:
        return None
    return v if math.isfinite(v) and v < 1e4 else None  # calc_engine.js IRR_MAX와 같은 상한


def moic(paid_in: float, events: list[float]) -> float | None:
    """회수 배수. 자기자본 투입액과 그 뒤의 개별 현금흐름(추가 출자는 음수)을 상계 없이 더한다."""
    out = paid_in + sum(-v for v in events if v < 0)
    back = sum(v for v in events if v > 0)
    return back / out if out > 0 else None


def develop(p: dict, equity: float) -> dict:
    """개발 단계를 한 번 돌린다. 준공 시 차입 잔액, 이자 합계, 월별 자기자본 투입을 돌려준다."""
    hard = p["gfa_py"] * p["cost_per_py"] / 10000.0
    soft = hard * p["soft_pct"] / 100.0
    extra = p["land"] * p["acq_pct"] / 100.0
    buy_fee = p["land"] * p.get("acq_fee", 0) / 100.0
    done = p["pre_m"] + p["con_m"]
    rate = p["dev_rate"] / 1200.0
    loan = used = interest_sum = 0.0
    draws = {}
    for m in range(1, done + 1):
        spend = soft / done
        if m == 1:
            spend += p["land"] + extra + buy_fee
        if m > p["pre_m"]:
            spend += hard / p["con_m"]
        interest = loan * rate
        interest_sum += interest
        need = spend + interest
        own = min(need, max(0.0, equity - used))
        used += own
        draws[m] = own
        loan += need - own
    base = p["land"] + extra + buy_fee + hard + soft
    return {"loan": loan, "interest": interest_sum, "draws": draws, "total": base + interest_sum,
            "hard": hard, "soft": soft, "extra": extra, "buy_fee": buy_fee, "base": base, "done": done}


def solve_equity(p: dict) -> float:
    """자기자본 = 비율 × 총사업비가 되는 자기자본. 차이가 자기자본에 대해 단조 감소하므로 구간을 반씩 줄인다."""
    share = p["equity_pct"] / 100.0
    low, high = 0.0, share * develop(p, 0.0)["total"]
    for _ in range(200):
        mid = (low + high) / 2.0
        if mid == low or mid == high:
            break
        if share * develop(p, mid)["total"] > mid:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def calc(p: dict, probe=None) -> dict:
    equity = solve_equity(p)
    dev = develop(p, equity)
    total, done = dev["total"], dev["done"]
    stab = done + p["lease_m"]

    rented = p["gfa_py"] * p.get("rent_area_pct", 100) / 100
    full_month = rented * p["rent"] / 10000.0 * (1 - p["vacancy"] / 100.0) * (1 - p["opex"] / 100.0)
    reit_month = total * p["reit_cost"] / 1200.0
    fee_month = total * p.get("mgmt_fee", 0) / 1200.0
    loan = dev["loan"]
    saved = lease_int = lease_noi = 0.0
    highest = loan
    for k in range(1, p["lease_m"] + 1):
        i = loan * p["dev_rate"] / 1200.0
        n = full_month * k / p["lease_m"]
        left = n - i - reit_month - fee_month
        if left < 0:
            loan += -left
        else:
            repay = min(loan, left)
            loan -= repay
            saved += left - repay
        lease_int += i
        lease_noi += n
        highest = max(highest, loan)

    noi1 = full_month * 12.0
    cap = p["exit_cap"] / 100.0
    value = noi1 / cap
    cost_rate = p["sale_cost"] / 100.0
    fee_rate = p.get("sale_fee", 0) / 100.0

    flow_a = {m: -v for m, v in dev["draws"].items()}
    net_a = value * (1 - cost_rate - fee_rate) - loan + saved
    flow_a[stab] = flow_a.get(stab, 0.0) + net_a

    perm = value * p["perm_ltv"] / 100.0
    threshold = p.get("perm_dscr_min", 1.2)
    yearly_reit = reit_month * 12
    yearly_fee = fee_month * 12
    constraint = "ltv"
    if threshold and p["perm_rate"]:
        affordable = max(noi1 - yearly_reit - yearly_fee, 0) * 100 / (threshold * p["perm_rate"])
        if affordable <= perm:
            constraint = "both" if affordable == perm else "dscr"
            perm = affordable
    switch = perm - loan + saved
    invested = equity - min(0, switch)
    flow_b = {m: -v for m, v in dev["draws"].items()}
    flow_b[stab] = flow_b.get(stab, 0.0) + switch
    yearly_int = perm * p["perm_rate"] / 100.0
    rows = []
    for y in range(1, p["hold_y"] + 1):
        noi = noi1 * (1 + p["growth"] / 100.0) ** (y - 1)
        spare = noi - yearly_reit - yearly_fee
        cf = spare - yearly_int
        t = stab + 12 * y
        flow_b[t] = flow_b.get(t, 0.0) + cf
        rows.append({"y": y, "noi": noi, "cf": cf, "dscr": spare / yearly_int if yearly_int > 0 else None})
    end = stab + 12 * p["hold_y"]
    sale_b = noi1 * (1 + p["growth"] / 100.0) ** p["hold_y"] / cap
    net_b = sale_b * (1 - cost_rate - fee_rate) - perm
    flow_b[end] = flow_b.get(end, 0.0) + net_b

    if probe is not None:
        plan, target = probe
        flows = flow_a if plan == "a" else flow_b
        return {"balance": switch if plan == "refi" else math.fsum(
            cf * (1 + target / 100) ** (-month / 12) for month, cf in flows.items())}

    return {
        "acq": dev["extra"], "acq_fee": dev["buy_fee"], "hard": dev["hard"], "soft": dev["soft"], "fin": dev["interest"], "tpc": total,
        "equity": equity, "dev_debt": dev["loan"], "dev_debt_peak": highest,
        "ltc": dev["loan"] / total if total > 0 else None,
        "debt_to_equity": highest / equity if equity > 0 else None,
        "Tc": done, "Ts": stab, "Tend": end,
        "lease_interest": lease_int, "lease_noi": lease_noi, "debt_at_stab": loan, "cash_at_stab": saved,
        "noi1": noi1, "value": value, "yield_on_cost": noi1 / total if total > 0 else None,
        "dev_margin": (value - total) / total if total > 0 else None,
        "a.sale": value, "a.net": net_a, "a.irr": yearly(month_irr(flow_a)), "a.multiple": moic(equity, [net_a]),
        "a.profit": sum(flow_a.values()),
        "a.fees": dev["buy_fee"] + fee_month * p["lease_m"] + value * fee_rate,
        "b.fees": dev["buy_fee"] + fee_month * p["lease_m"] + yearly_fee * p["hold_y"] + sale_b * fee_rate,
        "b.loan": perm, "b.refi": switch, "b.constraint": constraint, "b.paid_in": invested,
        "b.loan_to_equity": perm / invested if invested > 0 else None,
        "b.sale": sale_b, "b.net": net_b, "b.irr": yearly(month_irr(flow_b)),
        "b.multiple": moic(equity, [switch] + [r["cf"] for r in rows] + [net_b]),
        "b.profit": sum(flow_b.values()),
        "b.dscr_min": min(r["dscr"] for r in rows) if rows and yearly_int > 0 else None,
        "b.years.yield_on_equity": [r["cf"] / invested if invested else None for r in rows],
        "b.years.noi": [r["noi"] for r in rows], "b.years.cf": [r["cf"] for r in rows],
    }


def inverse(p, key, plan, target):
    """목표 할인율의 순현재가치가 0이 되는 구간을 4등분해 좁힌다."""
    lo, hi = {
        "land": (0, max(1000, 5 * p["land"])),
        "cost_per_py": (0, max(1000, 5 * p["cost_per_py"])),
        "rent": (0, max(100, 5 * p["rent"])),
        "exit_cap": (0.1, max(20, 5 * p["exit_cap"])),
    }[key]

    def balance(x):
        return calc(dict(p, **{key: x}), (plan, target))["balance"]

    left, right = balance(lo), balance(hi)
    if plan == "refi" and left >= 0:
        return lo
    if left * right > 0:
        return None
    while hi - lo > 1e-9 * max(1, abs(lo)):
        width = (hi - lo) / 4
        for x in (lo + width, lo + 2 * width, lo + 3 * width, hi):
            value = balance(x)
            if left * value <= 0:
                hi = x
                break
            lo, left = x, value
    if plan == "refi":
        return hi
    x = (lo + hi) / 2
    irr = calc(dict(p, **{key: x}))[plan + ".irr"]
    return x if irr is not None and abs(100 * irr - target) < 0.0001 else None
