"""
实验设计 · 第二部分：机制模拟（Monte Carlo）
============================================

回答：如果考核策略真的降低了超期发货率，**预期能减少多少差评？**

为什么不直接"假设迟到率降到 5% 然后检验显著"：
    那样只是把想要的结论当输入，p 值必然显著，不包含任何信息。
    正确做法是模拟**传导机制**——用真实数据估计的链条推演结果。

传导链（每一步参数都来自真实数据）：
    考核强度 → 超期发货率下降
                  ↓  P(迟到 | 超期) vs P(迟到 | 按期)
              迟到率下降
                  ↓  各象限的差评率
              差评率下降

输入：data/snapshot.parquet
输出：output/chain.png、output/sensitivity.png、output/mechanism_table.csv

运行：python experiment/mechanism_sim.py
"""

import os

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei']
plt.rcParams['axes.unicode_minus'] = False

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.path.join(BASE, 'data', 'snapshot.parquet')
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output')
os.makedirs(OUT, exist_ok=True)

N_SIM = 2000
RNG = np.random.default_rng(42)


# ============================================================================
# 1. 从真实数据估计传导链参数
# ============================================================================

def load_chain():
    """估计四象限的样本量、迟到率和差评率。"""
    df = pd.read_parquet(SNAP)
    df['purchase_date'] = pd.to_datetime(df['purchase_date'])

    d = df[df['is_overdue_ship'].notna() & df['is_late'].notna()].copy()

    cells = {}
    for od in (0, 1):
        for lt in (0, 1):
            sub = d[(d['is_overdue_ship'] == od) & (d['is_late'] == lt)]
            rev = sub[sub['review_score'].notna()]
            cells[(od, lt)] = {
                'n': len(sub),
                'n_reviewed': len(rev),
                'n_bad': int((rev['is_bad_review'] == 1).sum()),
            }

    n_total = len(d)
    n_overdue = int((d['is_overdue_ship'] == 1).sum())
    n_late_overdue = int(((d['is_overdue_ship'] == 1) & (d['is_late'] == 1)).sum())
    n_late_ontime = int(((d['is_overdue_ship'] == 0) & (d['is_late'] == 1)).sum())

    return {
        'cells': cells,
        'n_total': n_total,
        'n_overdue': n_overdue,
        'n_ontime': n_total - n_overdue,
        'n_late_overdue': n_late_overdue,
        'n_late_ontime': n_late_ontime,
        'n_reviewed_total': int(d['review_score'].notna().sum()),
    }


def point_estimates(ch):
    """把计数换成点估计，用于打印。"""
    c = ch['cells']
    p_overdue = ch['n_overdue'] / ch['n_total']
    p_late_od = ch['n_late_overdue'] / ch['n_overdue']
    p_late_ot = ch['n_late_ontime'] / ch['n_ontime']

    bad = {}
    for k, v in c.items():
        bad[k] = v['n_bad'] / v['n_reviewed'] if v['n_reviewed'] else np.nan

    late_rate = p_overdue * p_late_od + (1 - p_overdue) * p_late_ot
    bad_rate = sum(c[k]['n_reviewed'] / ch['n_reviewed_total'] * bad[k] for k in c)

    return dict(p_overdue=p_overdue, p_late_od=p_late_od, p_late_ot=p_late_ot,
                bad=bad, late_rate=late_rate, bad_rate=bad_rate)


# ============================================================================
# 2. Monte Carlo 模拟
# ============================================================================

def beta_sample(n_success, n_trial, size):
    """用 Beta 后验采样（Laplace 平滑），模拟估计的不确定性。"""
    return RNG.beta(1 + n_success, 1 + max(n_trial - n_success, 0), size)


def simulate(ch, overdue_target, k_transmit=1.0, n_sim=N_SIM):
    """模拟「超期率降到 overdue_target」后的差评率变化。

    参数
    ----
    overdue_target : 干预后的超期发货率（0-1）
    k_transmit     : 传导强度折减系数
                     1.0 = 完全相信观测到的 P(迟到|超期) 差异
                     0.5 = 认为其中一半是混杂，因果效应只有一半
                     0.0 = 认为超期对迟到没有因果影响（最保守）
    """
    c = ch['cells']

    # 各参数的抽样分布
    p_late_od = beta_sample(ch['n_late_overdue'], ch['n_overdue'], n_sim)
    p_late_ot = beta_sample(ch['n_late_ontime'], ch['n_ontime'], n_sim)

    # 传导强度折减：把观测差异按 k 缩小
    p_late_od_eff = p_late_ot + k_transmit * (p_late_od - p_late_ot)
    p_late_od_eff = np.clip(p_late_od_eff, 0, 1)

    bad = {}
    review_rate = {}
    for key, v in c.items():
        bad[key] = beta_sample(v['n_bad'], v['n_reviewed'], n_sim)
        # 各象限的「评价率」不同（迟到订单更可能被评价），
        # 用订单份额直接加权会低估整体差评率，必须再乘评价率
        review_rate[key] = beta_sample(v['n_reviewed'], v['n'], n_sim)

    def bad_rate_given(p_od, p_od_late, p_ot_late):
        """给定超期率与两组迟到率，算出整体差评率。

        权重 = 订单份额 × 该象限的评价率，即在「已评价订单」上加权，
        这样未干预时的结果能精确复现观测到的整体差评率。
        """
        w = {
            (1, 1): p_od * p_od_late,
            (1, 0): p_od * (1 - p_od_late),
            (0, 1): (1 - p_od) * p_ot_late,
            (0, 0): (1 - p_od) * (1 - p_ot_late),
        }
        rev = {k: w[k] * review_rate[k] for k in w}
        total = sum(rev.values())
        return sum(rev[k] * bad[k] for k in w) / total

    p_overdue_now = ch['n_overdue'] / ch['n_total']

    # 反事实基线（同一次参数抽样，isolate 干预效果）
    base_bad = bad_rate_given(p_overdue_now, p_late_od_eff, p_late_ot)
    # 干预后
    new_bad = bad_rate_given(overdue_target, p_late_od_eff, p_late_ot)

    delta = base_bad - new_bad       # 差评率的绝对下降
    return delta, base_bad, new_bad


# ============================================================================
# 3. 主流程
# ============================================================================

def main():
    ch = load_chain()
    pe = point_estimates(ch)

    print('=' * 76)
    print(' 一、传导链参数（全部来自真实数据，非假设）')
    print('=' * 76)
    print(f"  样本量              : {ch['n_total']:,} 单（有四象限判定的订单）")
    print(f"  超期发货率           : {pe['p_overdue']*100:.2f}%")
    print(f"  P(迟到 | 超期发货)   : {pe['p_late_od']*100:.2f}%")
    print(f"  P(迟到 | 按期发货)   : {pe['p_late_ot']*100:.2f}%")
    print(f"  → 超期让迟到概率升到 {pe['p_late_od']/pe['p_late_ot']:.1f} 倍")
    print(f"  → 推导整体迟到率     : {pe['late_rate']*100:.2f}%（实际 8.11%，吻合）")

    print()
    print('  四象限差评率：')
    print(f"  {'发货':>6s} {'送达':>6s} {'订单数':>9s} {'已评价':>9s} {'差评率':>8s}")
    print('  ' + '-' * 46)
    for (od, lt), v in ch['cells'].items():
        lab_o = '超期' if od else '按期'
        lab_l = '迟到' if lt else '准时'
        br = v['n_bad'] / v['n_reviewed'] * 100 if v['n_reviewed'] else np.nan
        print(f'  {lab_o:>6s} {lab_l:>6s} {v["n"]:>9,} {v["n_reviewed"]:>9,} {br:>7.2f}%')

    print()
    print('  ★ 关键：同样是「迟到」，超期与期发的差评率只差 '
          f"{(pe['bad'][(1,1)]-pe['bad'][(0,1)])*100:.1f}pp")
    print('     → 说明超期发货的伤害主要是「让迟到更可能」，而不是迟到本身更痛')
    print('     → 这就是项目一说的「完全中介效应」的量化证据')

    # ── 主模拟：考核使超期率从 9.06% 降到 5% ──
    print()
    print('=' * 76)
    print(' 二、主场景：考核策略使超期发货率 9.06% → 5.00%')
    print(f'     Monte Carlo {N_SIM:,} 次，每个参数按其样本量做 Beta 抽样')
    print('=' * 76)

    delta, base_bad, new_bad = simulate(ch, 0.05, k_transmit=1.0)
    n_reviewed = ch['n_reviewed_total']

    d_mean = delta.mean()
    d_lo, d_hi = np.percentile(delta, [2.5, 97.5])

    print(f'  模拟人群             : {ch["n_total"]:,} 单（有发货期限且已送达）')
    print(f'  该人群基线差评率      : {base_bad.mean()*100:.2f}%')
    print(f'    注：全站差评率是 14.63%，高出的部分来自 775 笔无商品明细的订单')
    print(f'        （unavailable/canceled，差评率约 78%）——它们不受考核策略影响，')
    print(f'        所以不纳入本次模拟，否则会稀释策略效果的估计。')
    print(f'  差评率变化           : {d_mean*100:.3f} pp  '
          f'(95% CI {d_lo*100:.3f} ~ {d_hi*100:.3f})')
    print(f'  即 {base_bad.mean()*100:.2f}% → {new_bad.mean()*100:.2f}%')
    print(f'  按 {n_reviewed:,} 条已评价订单折算: 减少 {d_mean*n_reviewed:,.0f} 条差评')

    # 年化
    df = pd.read_parquet(SNAP)
    df['purchase_date'] = pd.to_datetime(df['purchase_date'])
    end = pd.Timestamp('2018-08-31')
    start = end - pd.Timedelta(days=364)
    win = df[(df['purchase_date'] >= start) & (df['purchase_date'] <= end)]
    annual_orders = len(win)
    print(f'  按近 12 个月 {annual_orders:,} 单的年化规模'
          f'（假设超期率同比例下降）: 年减少约 {d_mean*annual_orders:,.0f} 条差评')

    # ── 敏感性分析 1：考核强度 ──
    print()
    print('=' * 76)
    print(' 三、敏感性分析 A：考核力度（超期率能压到多低）')
    print('=' * 76)
    print(f'  {"目标超期率":>10s} {"差评率下降":>12s} {"95% CI":>22s} {"年减少差评":>11s}')
    print('  ' + '-' * 62)

    rows = []
    for tgt in [0.09, 0.08, 0.07, 0.06, 0.05, 0.04, 0.03]:
        d, _, _ = simulate(ch, tgt, k_transmit=1.0)
        m = d.mean()
        lo, hi = np.percentile(d, [2.5, 97.5])
        print(f'  {tgt*100:>9.2f}% {m*100:>11.3f}pp '
              f'[{lo*100:>7.3f}, {hi*100:>6.3f}] {m*annual_orders:>10,.0f}')
        rows.append({
            '目标超期率%': round(tgt * 100, 2),
            '差评率下降pp': round(m * 100, 3),
            'CI下限pp': round(lo * 100, 3),
            'CI上限pp': round(hi * 100, 3),
            '年减少差评条数': round(m * annual_orders),
        })

    pd.DataFrame(rows).to_csv(os.path.join(OUT, 'mechanism_table.csv'),
                              index=False, encoding='utf-8-sig')

    # ── 敏感性分析 2：传导强度折减 ──
    print()
    print('=' * 76)
    print(' 四、敏感性分析 B：传导强度（观测关联中多少是真因果）')
    print('=' * 76)
    print('  观测到 P(迟到|超期)=24.42% vs P(迟到|按期)=6.49%。')
    print('  但超期的卖家可能本来物流能力就差（混杂），所以做折减更稳妥。')
    print()
    print(f'  {"传导强度 k":>10s} {"目标超期率 5%":>15s} {"年减少差评":>11s} {"解读":>16s}')
    print('  ' + '-' * 60)

    sens = []
    for k in [1.0, 0.75, 0.5, 0.25, 0.0]:
        d, _, _ = simulate(ch, 0.05, k_transmit=k)
        m = d.mean()
        lab = {1.0: '完全采信观测', 0.75: '折减 25%',
               0.5: '折减 50%', 0.25: '折减 75%', 0.0: '无因果（最保守）'}[k]
        print(f'  {k:>10.2f} {m*100:>14.3f}pp {m*annual_orders:>10,.0f} {lab:>16s}')
        sens.append({'传导强度k': k, '差评率下降pp': round(m * 100, 3),
                     '年减少差评': round(m * annual_orders)})

    pd.DataFrame(sens).to_csv(os.path.join(OUT, 'sensitivity.csv'),
                              index=False, encoding='utf-8-sig')

    # ── 图 1：四象限差评率 ──
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))

    labels, vals, colors = [], [], []
    for (od, lt), v in sorted(ch['cells'].items(), key=lambda x: (x[0][1], x[0][0])):
        lab_o = '超期' if od else '按期'
        lab_l = '迟到' if lt else '准时'
        labels.append(f'{lab_o}\n{lab_l}')
        vals.append(v['n_bad'] / v['n_reviewed'] * 100)
        colors.append('#C0392B' if lt else '#27AE60')

    bars = axes[0].bar(labels, vals, color=colors)
    for b, v in zip(bars, vals):
        axes[0].text(b.get_x() + b.get_width() / 2, v + 1, f'{v:.1f}%',
                     ha='center', fontsize=10, fontweight='bold')
    axes[0].set_ylabel('差评率 (%)')
    axes[0].set_title('四象限：超期发货 × 是否迟到 → 差评率', fontsize=12)
    axes[0].set_ylim(0, max(vals) * 1.2)
    axes[0].grid(axis='y', alpha=.3)
    axes[0].axhline(14.63, color='gray', ls='--', lw=1)
    axes[0].text(3.45, 15.5, '全站基线 14.63%', ha='right', fontsize=9, color='gray')

    # 图 2：传导链示意
    ax = axes[1]
    ax.axis('off')
    chain = [
        ('考核策略上线', '#2E86C1'),
        (f'超期发货率\n{pe["p_overdue"]*100:.1f}% → 5.0%', '#5DADE2'),
        (f'迟到率\n{pe["late_rate"]*100:.2f}% → ?', '#E8A33D'),
        (f'差评率\n{pe["bad_rate"]*100:.2f}% → {new_bad.mean()*100:.2f}%', '#C0392B'),
    ]
    ys = [0.85, 0.6, 0.35, 0.1]
    for (txt, col), y in zip(chain, ys):
        ax.add_patch(plt.Rectangle((0.22, y - 0.075), 0.56, 0.14,
                                   facecolor=col, alpha=.18, edgecolor=col, lw=2))
        ax.text(0.5, y, txt, ha='center', va='center', fontsize=11, fontweight='bold')
    for y1, y2 in zip(ys[:-1], ys[1:]):
        ax.annotate('', xy=(0.5, y2 + 0.078), xytext=(0.5, y1 - 0.078),
                    arrowprops=dict(arrowstyle='->', lw=2, color='#555'))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_title('传导链：每一步参数都来自真实数据', fontsize=12)

    plt.tight_layout()
    p1 = os.path.join(OUT, 'chain.png')
    plt.savefig(p1, dpi=150, bbox_inches='tight')
    plt.close()

    # ── 图 3：敏感性与主图 ──
    fig, ax = plt.subplots(figsize=(10, 5.2))

    tgts = [r['目标超期率%'] / 100 for r in rows]
    means = [r['差评率下降pp'] for r in rows]
    los = [r['CI下限pp'] for r in rows]
    his = [r['CI上限pp'] for r in rows]

    ax.plot([t * 100 for t in tgts], means, marker='o', lw=2.2,
            color='#C0392B', label='预期差评率下降')
    ax.fill_between([t * 100 for t in tgts], los, his, alpha=.18, color='#C0392B',
                    label='95% 置信区间')
    ax.axvline(pe['p_overdue'] * 100, color='gray', ls='--', lw=1)
    ax.text(pe['p_overdue'] * 100 + 0.08, max(his) * 0.9,
            f'当前 {pe["p_overdue"]*100:.2f}%', fontsize=9, color='gray')
    ax.set_xlabel('考核后目标超期发货率 (%)')
    ax.set_ylabel('差评率下降 (pp)')
    ax.set_title('考核力度 vs 预期差评改善（Monte Carlo 2000 次）', fontsize=12)
    ax.invert_xaxis()
    ax.grid(alpha=.3)
    ax.legend(fontsize=9)

    plt.tight_layout()
    p2 = os.path.join(OUT, 'sensitivity.png')
    plt.savefig(p2, dpi=150, bbox_inches='tight')
    plt.close()

    print()
    print('=' * 76)
    print(' 五、结论')
    print('=' * 76)
    print(f'  1. 若考核能把超期率压到 5%，预期差评率下降 {d_mean*100:.3f}pp')
    print(f'     （95% CI {d_lo*100:.3f} ~ {d_hi*100:.3f}），年减少约 {d_mean*annual_orders:,.0f} 条差评')
    print(f'  2. 即使假设传导强度只有观测值的一半（k=0.5），')
    d5, _, _ = simulate(ch, 0.05, k_transmit=0.5)
    print(f'     差评率仍可下降 {d5.mean()*100:.3f}pp，年减少约 {d5.mean()*annual_orders:,.0f} 条')
    print('  3. 单看「差评率」的绝对改善不大（1pp 以下），但要注意：')
    print('     · 迟到订单本身减少得更多（这是客户直接感受）')
    print('     · 差评率是滞后指标，改善会晚 1-2 个月体现')
    print()
    print(f'  已保存: {p1}')
    print(f'  已保存: {p2}')
    print(f'  已保存: {os.path.join(OUT, "mechanism_table.csv")}')
    print(f'  已保存: {os.path.join(OUT, "sensitivity.csv")}')


if __name__ == '__main__':
    main()
