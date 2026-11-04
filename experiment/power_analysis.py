"""
实验设计 · 第一部分：功效分析与样本量测算
==========================================

回答运营真正关心的问题：**要检测 X 个百分点的改善，实验要跑多久？**

为什么不用"假设结果再检验"的做法：
    如果先假设"迟到率会从 8.11% 降到 5%"，再用卡方检验验证，
    结果必然显著（5,000 单/组时 z≈6.3）——这不包含任何真实信息。
    实验的目的是**降低不确定性**，而不是确认预设结论。
    所以正确做法是先算：能检测出多大的效果、需要多少样本。

输入：data/snapshot.parquet（与看板同一份数据快照）
输出：output/power_table.csv、output/power_curve.png

运行：python experiment/power_analysis.py
"""

import os
import sys

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei']
plt.rcParams['axes.unicode_minus'] = False

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.path.join(BASE, 'data', 'snapshot.parquet')
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output')
os.makedirs(OUT, exist_ok=True)

ALPHA = 0.05
POWER = 0.80


# ============================================================================
# 1. 从真实数据算基线
# ============================================================================

def load_baseline():
    """从快照算出实验的基线指标。"""
    df = pd.read_parquet(SNAP)
    # 快照里的 purchase_date 存成了 date 对象，转成 datetime 才能做日期比较
    df['purchase_date'] = pd.to_datetime(df['purchase_date'])

    delivered = df['is_delivered'] == 1
    late_rate = df.loc[delivered, 'is_late'].mean()

    judged = df['is_overdue_ship'].notna()
    overdue_rate = (df.loc[judged, 'is_overdue_ship'] == 1).mean()

    reviewed = df['review_score'].notna()
    bad_rate = (df.loc[reviewed, 'is_bad_review'] == 1).mean()

    # 有订单的天数（仅用于展示数据密度）
    daily = df.groupby('purchase_date').size()

    # 日均订单：用「最近 12 个月的日历日均」，不是"有订单日"的均值
    #   · 实验周期按日历天算，用有订单日会高估产能、低估周期
    #   · 2016 年每月只有几十单，会严重拉低整体均值，不代表现在的业务量
    end = pd.Timestamp('2018-08-31')
    start = end - pd.Timedelta(days=364)
    window = df[(df['purchase_date'] >= start) & (df['purchase_date'] <= end)]
    avg_daily = len(window) / 365

    return {
        'late_rate': late_rate,
        'overdue_rate': overdue_rate,
        'bad_rate': bad_rate,
        'avg_daily_orders': avg_daily,
        'n_orders': len(df),
        'n_delivered': int(delivered.sum()),
        'n_reviewed': int(reviewed.sum()),
        'active_days': len(daily),
        'window_orders': len(window),
        'window_start': start.date(),
        'window_end': end.date(),
    }


# ============================================================================
# 2. 样本量公式（两独立比例检验，正态近似）
# ============================================================================

def n_per_group(p1, p2, alpha=ALPHA, power=POWER):
    """检测 p1 -> p2 的差异，每组所需样本量。"""
    if p1 == p2:
        return np.inf
    p_bar = (p1 + p2) / 2
    z_a = stats.norm.ppf(1 - alpha / 2)
    z_b = stats.norm.ppf(power)
    num = (z_a * np.sqrt(2 * p_bar * (1 - p_bar))
           + z_b * np.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2
    return num / (p1 - p2) ** 2


def power_at_n(p1, p2, n, alpha=ALPHA):
    """给定每组样本量 n，计算能检出的功效（power）。"""
    if p1 == p2:
        return 1.0
    p_bar = (p1 + p2) / 2
    se0 = np.sqrt(2 * p_bar * (1 - p_bar) / n)
    se1 = np.sqrt(p1 * (1 - p1) / n + p2 * (1 - p2) / n)
    z_a = stats.norm.ppf(1 - alpha / 2)
    return 1 - stats.norm.cdf((z_a * se0 - abs(p1 - p2)) / se1)


# ============================================================================
# 3. 主流程
# ============================================================================

def main():
    b = load_baseline()

    print('=' * 74)
    print(' 一、基线（来自真实数据）')
    print('=' * 74)
    print(f"  订单总数        : {b['n_orders']:,}")
    print(f"  已送达 / 已评价  : {b['n_delivered']:,} / {b['n_reviewed']:,}")
    print(f"  有订单天数      : {b['active_days']:,} 天")
    print(f"  近 12 个月单量   : {b['window_orders']:,} 单 "
          f"（{b['window_start']} ~ {b['window_end']}）")
    print(f"  日历日均订单     : {b['avg_daily_orders']:.1f} 单/天  ← 用于算实验周期")
    print(f"  迟到率（北极星）  : {b['late_rate']*100:.2f}%")
    print(f"  超期发货率      : {b['overdue_rate']*100:.2f}%")
    print(f"  差评率          : {b['bad_rate']*100:.2f}%")

    # ── 场景：考核策略使迟到率从基线下降 ──
    p1 = b['late_rate']
    targets = [7.5, 7.0, 6.5, 6.0, 5.0, 4.0]

    rows = []
    print()
    print('=' * 74)
    print(' 二、样本量与实验周期')
    print(f'     检验：两独立比例，α={ALPHA}，power={POWER}，日均 {b["avg_daily_orders"]:.0f} 单')
    print('=' * 74)
    print(f'  {"迟到率目标":>10s} {"绝对降幅":>10s} {"每组样本":>10s} '
          f'{"总样本":>10s} {"实验天数":>10s}')
    print('  ' + '-' * 60)

    for p2 in targets:
        n = n_per_group(p1, p2 / 100)
        if not np.isfinite(n):
            continue
        n = int(np.ceil(n))
        total = n * 2
        days = total / b['avg_daily_orders']
        print(f'  {p2:>9.1f}% {(p1*100-p2):>9.2f}pp {n:>10,} '
              f'{total:>10,} {days:>9.0f} 天')
        rows.append({
            '目标迟到率%': p2,
            '绝对降幅pp': round(p1 * 100 - p2, 2),
            '相对降幅%': round((p1 * 100 - p2) / (p1 * 100) * 100, 1),
            '每组样本': n,
            '总样本': total,
            '实验天数': round(days),
        })

    table = pd.DataFrame(rows)
    table.to_csv(os.path.join(OUT, 'power_table.csv'), index=False, encoding='utf-8-sig')

    # ── 关键结论 ──
    print()
    print('=' * 74)
    print(' 三、结论')
    print('=' * 74)
    n_1pp = n_per_group(p1, p1 - 0.01)
    days_1pp = np.ceil(n_1pp) * 2 / b['avg_daily_orders']
    print(f'  检测「迟到率下降 1 个百分点」（{p1*100:.2f}% → {p1*100-1:.2f}%）:')
    print(f'    每组需要 {int(np.ceil(n_1pp)):,} 单，单组跑 {np.ceil(n_1pp)/b["avg_daily_orders"]:.0f} 天')
    print(f'    两组并行（各占 50% 流量）需 约 {days_1pp:.0f} 天 ≈ {days_1pp/30:.1f} 个月')
    print()
    print('  → 这是运营必须知道的现实：小效果的实验周期长到不可接受。')
    print('  → 所以策略上线前应该先定「可接受的最小效果」，反推实验周期是否可行。')

    # ── 功效曲线 ──
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))

    ns = np.arange(200, 12001, 200)
    for p2 in [7.0, 6.0, 5.0]:
        powers = [power_at_n(p1, p2 / 100, n) for n in ns]
        axes[0].plot(ns, powers, lw=2, label=f'{p1*100:.2f}% → {p2:.1f}%')
    axes[0].axhline(POWER, color='gray', ls='--', lw=1, label=f'power={POWER}')
    axes[0].axhline(ALPHA, color='#C0392B', ls=':', lw=1, label=f'α={ALPHA}')
    axes[0].set_xlabel('每组样本量')
    axes[0].set_ylabel('功效 (power)')
    axes[0].set_title('功效曲线：样本量 vs 检出能力', fontsize=12)
    axes[0].legend(fontsize=9)
    axes[0].grid(alpha=.3)
    axes[0].set_ylim(0, 1.02)

    # 右图：降幅 vs 所需天数
    deltas = np.arange(0.5, 4.01, 0.25)
    days = [np.ceil(n_per_group(p1, p1 - d / 100)) * 2 / b['avg_daily_orders']
            for d in deltas]
    axes[1].plot(deltas, days, marker='o', lw=2, color='#2E86C1')
    axes[1].set_xlabel('期望检测的绝对降幅 (pp)')
    axes[1].set_ylabel('实验周期（天，两组各 50% 流量）')
    axes[1].set_title('降幅越小，实验周期越长（超线性）', fontsize=12)
    axes[1].grid(alpha=.3)
    axes[1].axhline(90, color='#C0392B', ls='--', lw=1)
    axes[1].text(2.6, 95, '90 天（一个季度）', color='#C0392B', fontsize=9)

    plt.tight_layout()
    path = os.path.join(OUT, 'power_curve.png')
    plt.savefig(path, dpi=150, bbox_inches='tight')
    plt.close()
    print()
    print(f'  已保存: {path}')
    print(f'  已保存: {os.path.join(OUT, "power_table.csv")}')


if __name__ == '__main__':
    main()
