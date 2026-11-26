/* 프로젝트리츠 사업성 간이 계산 엔진.
 *
 * 개발(토지 취득 → 착공 전 → 공사) → 임대 안정화 → A안: 안정화 시점 매각 / B안: 담보대출로 바꿔 운영한 뒤 매각.
 * 금액 단위는 억원, 기간 단위는 개월(운영 보유 기간만 년). 같은 입력이면 항상 같은 결과가 나온다.
 * 자산관리회사 보수(매입, 운용, 매각)는 비용으로 빼고, 사업 기간의 보수 합계를 따로 돌려준다.
 * 화면(calc.html)과 검산(tests/calc_check.py)이 이 파일 하나를 같이 쓴다.
 */

/* 월별 현금흐름(cfs[m] = m개월째)의 월 내부수익률. 부호가 한쪽뿐이면 null.
 * 양의 금리는 현재가치, 음의 금리는 미래가치로 부호를 비교한다. */
function irrMonthly(cfs) {
  const first = cfs.findIndex(v => v !== 0);
  let last = cfs.length - 1, changes = 0, previous = 0;
  for (const v of cfs) {
    if (!v) continue;
    if (previous && Math.sign(v) !== previous) changes++;
    previous = Math.sign(v);
  }
  if (!changes) return null;
  while (!cfs[last]) last--;
  const balance = r => {
    let sum = 0;
    if (r > 0) {
      for (let i = last; i >= first; i--) sum = cfs[i] + sum / (1 + r);
    } else {
      for (let i = first; i <= last; i++) sum = cfs[i] + sum * (1 + r);
    }
    return sum;
  };
  let lo = changes === 1 ? -1 : -0.9, hi = 1, flo = balance(lo);
  while (Math.sign(balance(hi)) === Math.sign(flo) && flo !== 0) {
    hi *= 2;
    if (changes !== 1 || !Number.isFinite(hi)) return null;
  }
  for (let k = 0; k < 200; k++) {
    const mid = (lo + hi) / 2, fm = balance(mid);
    if (!fm) return mid;
    if (mid === lo || mid === hi) break;
    if (Math.sign(flo) !== Math.sign(fm)) hi = mid;
    else { lo = mid; flo = fm; }
  }
  return (lo + hi) / 2;
}

// 17차 검토: 극단 입력에서 Infinity나 1e+131% 같은 값이 화면에 나가지 않게 연 1,000,000% 미만만 돌려준다
// (tests/calc_ref.py yearly와 같은 규칙. 역산은 IRR이 아니라 현재가치 부호로 풀어 영향이 없다)
const IRR_MAX = 1e4;
function annualize(rm) { if (rm == null) return null; const v = Math.pow(1 + rm, 12) - 1; return Number.isFinite(v) && v < IRR_MAX ? v : null; }

/* 회수 배수 = 받은 돈 합계 ÷ 넣은 돈 합계. flows는 [넣은 돈(음수) 또는 받은 돈(양수)]의 목록. */
function multiple(flows) {
  let pos = 0, neg = 0;
  for (const v of flows) { if (v > 0) pos += v; else neg -= v; }
  return neg > 0 ? pos / neg : null;
}

function reitCalc(p, probe) {
  const MAN = 1e-4;                                   // 만원 → 억원
  const hard = p.gfa_py * p.cost_per_py * MAN;        // 공사비
  const soft = hard * p.soft_pct / 100;               // 설계, 감리, 인허가, 예비비 등
  const acq = p.land * p.acq_pct / 100;               // 토지 취득 부대비용
  const acqFee = p.land * (p.acq_fee ?? 0) / 100;     // 자산관리회사 매입보수(토지 취득 때 한 번)
  const Tc = p.pre_m + p.con_m;                       // 준공 시점(개월)
  const Ts = Tc + p.lease_m;                          // 임대 안정화 시점
  const rDev = p.dev_rate / 100 / 12;
  const baseCost = p.land + acq + acqFee + hard + soft;
  const share = p.equity_pct / 100;

  // 월별 사업비: 토지, 부대비용, 매입보수는 첫 달, 기타 사업비는 준공까지 균등, 공사비는 공사기간에 균등
  const cost = new Array(Tc + 1).fill(0);
  cost[1] += p.land + acq + acqFee;
  for (let m = 1; m <= Tc; m++) cost[m] += soft / Tc;
  for (let m = p.pre_m + 1; m <= Tc; m++) cost[m] += hard / p.con_m;

  // 개발 단계 조달: 자기자본을 먼저 쓰고 나머지는 차입. 이자는 차입 원금에 더한다(준공까지의 이자 = 금융비).
  function devRun(E) {
    let debt = 0, eq = 0, fin = 0;
    const eqDraw = new Array(Tc + 1).fill(0), debtBal = new Array(Ts + 1).fill(0);
    for (let m = 1; m <= Tc; m++) {
      const interest = debt * rDev;
      fin += interest;
      const need = cost[m] + interest;
      const fromEq = Math.min(need, Math.max(0, E - eq));
      eq += fromEq; eqDraw[m] = fromEq;
      debt += need - fromEq; debtBal[m] = debt;
    }
    return { debt, fin, eqDraw, debtBal, tpc: baseCost + fin };
  }
  // 자기자본 = 총사업비 × 자기자본 비율. 자기자본이 늘면 이자가 줄어 총사업비가 줄어드므로
  // (비율 × 총사업비 − 자기자본)은 자기자본에 대해 단조 감소한다. 이분법으로 0이 되는 값을 찾는다.
  let eLo = 0, eHi = share * devRun(0).tpc;
  for (let i = 0; i < 200; i++) {
    const mid = (eLo + eHi) / 2;
    if (mid === eLo || mid === eHi) break;
    if (share * devRun(mid).tpc - mid > 0) eLo = mid; else eHi = mid;
  }
  const E = (eLo + eHi) / 2, run = devRun(E);
  const tpc = run.tpc, devDebt = run.debt;

  // 임대 안정화: 입주율이 매달 같은 폭으로 올라간다. 임대 순수익으로 이자, 리츠 운영비용, 운용보수를 내고,
  // 모자라면 차입이 늘고 남으면 차입을 갚는다. 차입을 다 갚고도 남는 돈은 이자 없이 쌓아 두었다가 안정화 시점에 받는다.
  const rentArea = p.gfa_py * (p.rent_area_pct ?? 100) / 100;
  const noiMonthFull = rentArea * p.rent * MAN * (1 - p.vacancy / 100) * (1 - p.opex / 100);
  const reitMonth = tpc * p.reit_cost / 100 / 12;
  const feeMonth = tpc * (p.mgmt_fee ?? 0) / 100 / 12;  // 자산관리회사 운용보수(준공 다음 달부터)
  let debt = devDebt, cash = 0, leaseInterest = 0, leaseNoi = 0, peak = devDebt;
  for (let k = 1; k <= p.lease_m; k++) {
    const interest = debt * rDev, noi = noiMonthFull * k / p.lease_m;
    const net = noi - interest - reitMonth - feeMonth;  // 양수면 남는 돈
    if (net >= 0) { const pay = Math.min(debt, net); debt -= pay; cash += net - pay; }
    else debt -= net;
    leaseInterest += interest; leaseNoi += noi;
    run.debtBal[Tc + k] = debt;
    if (debt > peak) peak = debt;
  }
  const debtAtStab = debt, cashAtStab = cash;
  const noi1 = noiMonthFull * 12;                     // 안정화 첫해 순영업수익(NOI)
  const cap = p.exit_cap / 100, g = p.growth / 100, saleCost = p.sale_cost / 100, saleFee = (p.sale_fee ?? 0) / 100;
  const leaseFee = feeMonth * p.lease_m;              // 임대 안정화 기간의 운용보수 합계
  const value = noi1 / cap;                           // 안정화 시점 가치

  // A안: 안정화 시점에 매각
  const cfA = new Array(Ts + 1).fill(0);
  for (let m = 1; m <= Tc; m++) cfA[m] -= run.eqDraw[m];
  const netA = value * (1 - saleCost - saleFee) - debtAtStab + cashAtStab;
  cfA[Ts] += netA;
  const flowsA = [-E, netA];

  // B안: 안정화 시점에 담보대출로 바꾸고 hold_y년 운영한 뒤 매각(대출은 이자만 내고 만기에 일시 상환)
  const reitY = tpc * p.reit_cost / 100, feeY = feeMonth * 12, minDscr = p.perm_dscr_min ?? 1.2;
  const ltvLimit = value * p.perm_ltv / 100;
  const dscrLimit = minDscr > 0 && p.perm_rate > 0
    ? Math.max(0, noi1 - reitY - feeY) / (p.perm_rate / 100 * minDscr) : Infinity;
  const loan = Math.min(ltvLimit, dscrLimit);
  const constraint = ltvLimit === dscrLimit ? 'both' : loan === dscrLimit ? 'dscr' : 'ltv';
  const refi = loan - debtAtStab + cashAtStab;        // 양수면 자기자본 회수, 음수면 추가 출자
  const paidIn = E + Math.max(0, -refi);
  const Tend = Ts + 12 * p.hold_y;
  const cfB = new Array(Tend + 1).fill(0);
  for (let m = 1; m <= Tc; m++) cfB[m] -= run.eqDraw[m];
  cfB[Ts] += refi;
  const flowsB = [-E, refi];
  const interestY = loan * p.perm_rate / 100;
  const years = [];
  for (let y = 1; y <= p.hold_y; y++) {
    const noi = noi1 * Math.pow(1 + g, y - 1), cf = noi - interestY - reitY - feeY;
    cfB[Ts + 12 * y] += cf;
    flowsB.push(cf);
    years.push({ y, noi, interest: interestY, reit: reitY, fee: feeY, cf, dscr: interestY > 0 ? (noi - reitY - feeY) / interestY : null, yield_on_equity: paidIn > 0 ? cf / paidIn : null });
  }
  const saleB = noi1 * Math.pow(1 + g, p.hold_y) / cap;
  const netB = saleB * (1 - saleCost - saleFee) - loan;
  cfB[Tend] += netB;
  flowsB.push(netB);

  if (probe) return probe(cfA, cfB, refi);

  return {
    land: p.land, acq, acq_fee: acqFee, hard, soft, fin: run.fin, tpc, equity: E,
    dev_debt: devDebt, dev_debt_peak: peak, ltc: tpc > 0 ? devDebt / tpc : null,
    debt_to_equity: E > 0 ? peak / E : null,
    Tc, Ts, Tend,
    lease_interest: leaseInterest, lease_noi: leaseNoi, lease_fee: leaseFee, debt_at_stab: debtAtStab, cash_at_stab: cashAtStab,
    noi1, value, yield_on_cost: tpc > 0 ? noi1 / tpc : null, dev_margin: tpc > 0 ? (value - tpc) / tpc : null,
    // 자산관리회사 보수 합계(사업 기간) = 매입보수 + 준공 뒤 운용보수 + 매각보수
    a: { sale: value, net: netA, irr: annualize(irrMonthly(cfA)), multiple: multiple(flowsA), profit: cfA.reduce((s, v) => s + v, 0),
         sale_fee: value * saleFee, fees: acqFee + leaseFee + value * saleFee },
    b: { loan, refi, constraint, paid_in: paidIn, loan_to_equity: paidIn > 0 ? loan / paidIn : null, sale: saleB, net: netB, years,
         mgmt_fee_y: feeY, sale_fee: saleB * saleFee, fees: acqFee + leaseFee + feeY * p.hold_y + saleB * saleFee,
         irr: annualize(irrMonthly(cfB)), multiple: multiple(flowsB), profit: cfB.reduce((s, v) => s + v, 0),
         dscr_min: years.length && interestY > 0 ? Math.min(...years.map(x => x.dscr)) : null },
    eq_draw: run.eqDraw, debt_bal: run.debtBal, cf_a: cfA, cf_b: cfB,
  };
}

// 목표는 연 % 단위다. 임대료가 오르면 회수액이 늘고, 비용·Cap Rate가 오르면
// 줄어드는 구간을 전제로 한다. B안은 대출 한도 전환과 현금흐름의 부호 변화로
// 단조성이 깨질 수 있다. 양 끝 사이의 해 하나를 찾고 해당 방안의 IRR로 확인한다.
function solveFor(p, key, plan, target = p.target_irr ?? 8) {
  const floors = {land: 1000, cost_per_py: 1000, rent: 100, exit_cap: 20};
  let lo = key === 'exit_cap' ? 0.1 : 0;
  let hi = Math.max(floors[key], 5 * p[key]);
  if (!Number.isFinite(hi) || !Number.isFinite(target) || target <= -100) return null;
  const monthly = (1 + target / 100) ** (1 / 12);
  function probe(cfA, cfB, refi) {
    if (plan === 'refi') return refi;
    const flows = plan === 'a' ? cfA : cfB;
    // IRR이 없는 구간도 목표 수익률로 할인한 현금흐름의 부호는 구할 수 있다.
    let sum = 0;
    if (monthly >= 1) {
      for (let i = flows.length - 1; i >= 0; i--) sum = flows[i] + sum / monthly;
    } else {
      for (const flow of flows) sum = sum * monthly + flow;
    }
    return sum;
  }
  const at = x => reitCalc({...p, [key]: x}, probe);
  const left = at(lo), right = at(hi);
  if (!Number.isFinite(left) || !Number.isFinite(right)) return null;
  if (plan === 'refi' && left >= 0) return lo;
  if (left === 0) hi = lo;
  else if (right !== 0 && Math.sign(left) === Math.sign(right)) return null;
  for (let i = 0; i < 32 && hi > lo; i++) {
    const mid = (lo + hi) / 2, value = at(mid);
    if (!Number.isFinite(value)) return null;
    if (value === 0 || Math.sign(value) !== Math.sign(left)) hi = mid;
    else lo = mid;
  }
  if (plan === 'refi') return hi;
  const irr = reitCalc({...p, [key]: hi})[plan].irr;
  return irr != null && Math.abs(irr * 100 - target) < 0.0001 ? hi : null;
}

if (typeof module !== 'undefined') module.exports = { reitCalc, irrMonthly, annualize, multiple, solveFor };
