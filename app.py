"""
Olist 电商运营监控看板
======================
数据源：PostgreSQL 的 olist schema（视图 v_daily_ops_metrics / v_metrics_by_dim）

启动：
    streamlit run app.py

功能：
    ① 侧边栏：日期范围 / 州 / 品类 筛选
    ② 六个核心 KPI（带环比）
    ③ 趋势图（订单量 + 差评率双轴）
    ④ 异常预警（二项 z-score，非固定阈值）
    ⑤ 维度下钻（州 / 品类 / 州×品类）
"""

import os

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import create_engine, text

# ============================================================================
# 配置
# ============================================================================

st.set_page_config(
    page_title='Olist 运营监控看板',
    page_icon='📊',
    layout='wide',
    initial_sidebar_state='expanded',
)

# 本地默认连接（可用环境变量或 .streamlit/secrets.toml 覆盖）
DEFAULT_DB_URL = 'postgresql+psycopg2://postgres:123456@127.0.0.1:5432/analysis'

# 云端用的数据快照（由 scripts/export_snapshot.py 生成）
SNAPSHOT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'data', 'snapshot.parquet'
)

RAW_COLS = [
    'purchase_date', 'order_total', 'is_delivered', 'is_late',
    'is_overdue_ship', 'review_score', 'is_bad_review', 'prep_days',
    'customer_state', 'main_category',
]

# 数据起止（与 v_daily_ops_metrics 的日期骨架一致）
DATA_START = pd.Timestamp('2016-09-04').date()
DATA_END = pd.Timestamp('2018-10-17').date()

# 默认展示区间：排除两端数据稀疏的月份
# 2016 年每月只有几十单，2018-09/10 数据集未收全（合计仅 20 单），
# 直接看全区间会让 KPI 失真（末 7 天只有 7 单、准时率算不出来）
DEFAULT_START = pd.Timestamp('2017-01-01').date()
DEFAULT_END = pd.Timestamp('2018-08-31').date()

# 告警阈值（依据：实测 z 的标准差为 1.33，用 2σ 会导致 11.8% 天数告警，过吵）
Z_WARN = 2.5      # 关注
Z_ALERT = 3.0     # 严重
MIN_SAMPLE = 30   # 样本量下限

COLOR_ALERT = {'严重': '#C0392B', '关注': '#E8A33D', '正常': '#27AE60', '样本不足': '#9CA3AF'}


# ============================================================================
# 数据访问：本地连库 / 云端读快照（双模式）
# ============================================================================

def _db_url():
    """连接串优先级：Streamlit secrets > 环境变量 > 本地默认。"""
    try:
        if 'DATABASE_URL' in st.secrets:            # 云端可配 secrets，不写进代码
            return st.secrets['DATABASE_URL']
    except Exception:
        pass                                        # 没有 secrets.toml 是正常情况
    return os.environ.get('DATABASE_URL', DEFAULT_DB_URL)


@st.cache_resource
def get_engine():
    """能连上数据库就返回 engine；连不上返回 None（走快照）。"""
    try:
        eng = create_engine(
            _db_url(), pool_pre_ping=True,
            connect_args={'connect_timeout': 3},    # 云端连 localhost 会快速失败，不卡页面
        )
        with eng.connect() as conn:
            conn.execute(text('SELECT 1'))
        return eng
    except Exception:
        return None


@st.cache_data(ttl=3600, show_spinner='加载数据中…')
def load_raw():
    """加载订单级明细（10 列）。

    本地：从 PostgreSQL 的 v_pbi_orders 读
    云端：Streamlit Cloud 访问不到本机数据库，改读随仓库提交的 parquet 快照
    两种模式返回的 DataFrame 结构完全一致，上层逻辑无需区分。
    """
    eng = get_engine()

    if eng is not None:
        sql = f"SELECT {', '.join(RAW_COLS)} FROM olist.v_pbi_orders"
        with eng.connect() as conn:
            df = pd.read_sql(text(sql), conn)
        source = 'db'
    else:
        if not os.path.exists(SNAPSHOT_PATH):
            st.error(
                '无法连接数据库，且找不到数据快照 data/snapshot.parquet。\n\n'
                '本地请确认 PostgreSQL 已启动；'
                '云端请确认已运行 scripts/export_snapshot.py 并把快照提交到仓库。'
            )
            st.stop()
        df = pd.read_parquet(SNAPSHOT_PATH)
        source = 'snapshot'

    df['purchase_date'] = pd.to_datetime(df['purchase_date'])
    df.attrs['source'] = source
    # 打印到服务端控制台，便于排查「云端到底读的哪个数据源」
    print(f'[data] source={source}  rows={len(df):,}  '
          f'range={df["purchase_date"].min().date()} ~ {df["purchase_date"].max().date()}')
    return df


def data_source_label():
    return '数据库（实时）' if get_engine() is not None else '数据快照（parquet）'


def _agg_daily(d):
    """把订单级明细聚合成日级指标。"""
    g = d.groupby('purchase_date')
    return pd.DataFrame({
        'gmv':       g['order_total'].sum(),
        'orders':    g['order_total'].size(),
        'aov':       g['order_total'].mean(),
        'delivered': g['is_delivered'].sum(),
        'late':      g['is_late'].sum(),
        'judgeable': g['is_overdue_ship'].count(),
        'overdue':   g['is_overdue_ship'].sum(),
        'reviewed':  g['review_score'].count(),
        'bad':       g['is_bad_review'].sum(),
        'avg_score': g['review_score'].mean(),
        'avg_prep':  g['prep_days'].mean(),
    })


def _slice(df, start, end, states=None, categories=None):
    """按日期 + 可选维度切数据。"""
    m = ((df['purchase_date'] >= pd.Timestamp(start)) &
         (df['purchase_date'] <= pd.Timestamp(end)))
    if states:
        m &= df['customer_state'].isin(states)
    if categories:
        m &= df['main_category'].isin(categories)
    return df[m]


def load_daily(start, end, states=None, categories=None):
    """返回日级指标 DataFrame（已含比率、移动平均、z-score、告警等级）。"""
    df = _agg_daily(_slice(load_raw(), start, end, states, categories))

    # 补齐完整日期骨架（否则趋势图会有缺口，和项目一的 140 天问题同源）
    df = df.reindex(pd.date_range(start, end, freq='D'))
    df.index.name = 'dt'
    for c in ['gmv', 'orders', 'delivered', 'late', 'judgeable',
              'overdue', 'reviewed', 'bad']:
        df[c] = df[c].fillna(0)
    df = df.reset_index()

    # ── 计算比率（分母为 0 时保持 NaN，不写 0）──
    df['on_time_rate'] = np.where(df['delivered'] > 0,
                                  1 - df['late'] / df['delivered'], np.nan)
    df['overdue_ship_rate'] = np.where(df['judgeable'] > 0,
                                       df['overdue'] / df['judgeable'], np.nan)
    df['bad_review_rate'] = np.where(df['reviewed'] > 0,
                                     df['bad'] / df['reviewed'], np.nan)

    # ── 画图专用掩码列：样本量不足时置 NaN，避免小样本把 Y 轴压扁 ──
    # 项目一踩过同样的坑：2016 年某天只有 1 单且是差评 -> 100%，趋势图被拉爆
    df['bad_rate_plot'] = df['bad_review_rate'].where(df['reviewed'] >= MIN_SAMPLE)
    df['on_time_plot'] = df['on_time_rate'].where(df['delivered'] >= MIN_SAMPLE)
    df['overdue_plot'] = df['overdue_ship_rate'].where(df['judgeable'] >= MIN_SAMPLE)
    df['score_plot'] = df['avg_score'].where(df['reviewed'] >= MIN_SAMPLE)

    # ── 28 日加权基准 + 二项 z-score ──
    roll_bad = df['bad'].rolling(28, min_periods=7).sum()
    roll_rev = df['reviewed'].rolling(28, min_periods=7).sum()
    df['base28'] = roll_bad / roll_rev.replace(0, np.nan)

    se = np.sqrt(df['base28'] * (1 - df['base28']) / df['reviewed'])
    df['bad_z'] = (df['bad_review_rate'] - df['base28']) / se.replace(0, np.nan)

    # ── 移动平均 ──
    # 均线必须用「掩码后」的序列算：否则 1 单 100% 这种小样本日
    # 会把 7 日均线直接拉到 100%，图上出现假的尖峰
    df['bad_rate_ma7'] = df['bad_rate_plot'].rolling(7, min_periods=3).mean()
    df['orders_ma7'] = df['orders'].rolling(7, min_periods=3).mean()

    # ── 告警等级 ──
    def _level(row):
        if pd.isna(row['bad_review_rate']) or row['reviewed'] < MIN_SAMPLE:
            return '样本不足'
        z = row['bad_z']
        if pd.isna(z):
            return '样本不足'
        if abs(z) >= Z_ALERT:
            return '严重'
        if abs(z) >= Z_WARN:
            return '关注'
        return '正常'

    df['alert'] = df.apply(_level, axis=1)
    return df


@st.cache_data(ttl=3600)
def load_dims(start, end):
    """下拉框的可选值（按订单量降序）。"""
    d = _slice(load_raw(), start, end)
    states = (d['customer_state'].dropna().value_counts()
              .rename_axis('v').reset_index(name='n'))
    cats = (d['main_category'].dropna().value_counts()
            .rename_axis('v').reset_index(name='n'))
    return states, cats


def load_drill(start, end, level, states=None, categories=None):
    """维度下钻：按指定粒度聚合。"""
    dim = {'州': 'customer_state', '品类': 'main_category'}[level]
    d = _slice(load_raw(), start, end, states, categories)
    d = d[d[dim].notna()]

    g = d.groupby(dim)
    out = pd.DataFrame({
        '订单数':       g['order_total'].size(),
        'GMV':        g['order_total'].sum().round(0),
        '客单价':       g['order_total'].mean().round(2),
        '差评率':      (g['is_bad_review'].sum() /
                      g['review_score'].count().replace(0, np.nan) * 100).round(2),
        '迟到率':      (g['is_late'].sum() /
                      g['is_delivered'].sum().replace(0, np.nan) * 100).round(2),
        '平均备货天数':  g['prep_days'].mean().round(2),
    }).reset_index().rename(columns={dim: '维度'})

    return out.sort_values('订单数', ascending=False)


def load_alert_categories(dates):
    """一次性算出多个异常日各自的「问题品类」。"""
    empty = pd.DataFrame(columns=['dt', 'cat', 'bad', 'bad_rate', 'n'])
    if not dates:
        return empty

    raw = load_raw()
    d = raw[raw['purchase_date'].isin(pd.to_datetime(list(dates)))]
    d = d[d['main_category'].notna()]
    if d.empty:
        return empty

    g = d.groupby(['purchase_date', 'main_category'])
    agg = pd.DataFrame({
        'n':        g['order_total'].size(),
        'bad':      g['is_bad_review'].sum(),
        'reviewed': g['review_score'].count(),
    }).reset_index().rename(columns={'purchase_date': 'dt', 'main_category': 'cat'})

    agg = agg[agg['n'] >= 20]                       # 样本门槛
    if agg.empty:
        return empty

    agg['bad_rate'] = agg['bad'] / agg['reviewed'].replace(0, np.nan)
    # 按「贡献的差评条数」排序，而不是按比率——
    # 比率高可能只是小样本噪音，运营要的是「哪块贡献了最多差评」
    agg = (agg.sort_values('bad', ascending=False)
              .groupby('dt', as_index=False)
              .first())
    return agg[['dt', 'cat', 'bad', 'bad_rate', 'n']]


# ============================================================================
# 侧边栏
# ============================================================================

st.sidebar.title('📊 筛选器')

date_range = st.sidebar.date_input(
    '日期范围',
    value=(DEFAULT_START, DEFAULT_END),
    min_value=DATA_START,
    max_value=DATA_END,
    help='默认已排除两端数据稀疏的月份；需要看全量可手动拉到 2016-09-04 ~ 2018-10-17',
)
start = date_range[0] if len(date_range) > 0 else DATA_START
end = date_range[1] if len(date_range) > 1 else DATA_END

states_df, cats_df = load_dims(DATA_START, DATA_END)

sel_states = st.sidebar.multiselect(
    '州（默认全部）',
    options=states_df['v'].tolist(),
    format_func=lambda x: f'{x}（{int(states_df.loc[states_df.v == x, "n"].iloc[0]):,} 单）',
)
sel_cats = st.sidebar.multiselect(
    '品类（默认全部）',
    options=cats_df['v'].tolist(),
    format_func=lambda x: f'{x}（{int(cats_df.loc[cats_df.v == x, "n"].iloc[0]):,} 单）',
)

df = load_daily(start, end, sel_states or None, sel_cats or None)

st.sidebar.markdown('---')
st.sidebar.caption(
    f'数据范围：{df["dt"].min():%Y-%m-%d} ~ {df["dt"].max():%Y-%m-%d}　'
    f'共 {len(df)} 天'
)
st.sidebar.caption(f'数据源：{data_source_label()}')
if sel_states or sel_cats:
    st.sidebar.info('已应用维度筛选。注意：此时数字为该子集口径，不再等于全站口径。')

# ============================================================================
# 标题 + KPI
# ============================================================================

st.title('Olist 电商运营监控看板')
st.caption('数据源：PostgreSQL · olist schema　｜　'
           '口径定义见 metric_dictionary.md')

def _delta(cur, prev, pct=True, good_up=True):
    """生成 st.metric 的 delta 文本与颜色。"""
    if pd.isna(cur) or pd.isna(prev) or prev == 0:
        return None
    d = (cur - prev) / prev * 100 if pct else cur - prev
    unit = '%' if pct else ''
    sign = '+' if d >= 0 else ''
    return f'{sign}{d:.1f}{unit}'

# 用「最近 7 个有订单的日子」而不是最近 7 个日历日——
# 数据集末尾（2018-10）只有十几单，直接取 df.tail(7) 会让所有 KPI 变成 0
active = df[df['orders'] > 0].reset_index(drop=True)
tail7 = active.tail(7)
prev7 = active.iloc[-14:-7] if len(active) >= 14 else active.head(0)

def _sum(s, col):
    return s[col].sum() if len(s) else np.nan

gmv_now, gmv_prev = _sum(tail7, 'gmv'), _sum(prev7, 'gmv')
ord_now, ord_prev = _sum(tail7, 'orders'), _sum(prev7, 'orders')
aov_now = gmv_now / ord_now if ord_now else np.nan
aov_prev = gmv_prev / ord_prev if ord_prev else np.nan
bad_now = _sum(tail7, 'bad') / _sum(tail7, 'reviewed') if _sum(tail7, 'reviewed') else np.nan
bad_prev = _sum(prev7, 'bad') / _sum(prev7, 'reviewed') if _sum(prev7, 'reviewed') else np.nan
ot_now = 1 - _sum(tail7, 'late') / _sum(tail7, 'delivered') if _sum(tail7, 'delivered') else np.nan
ot_prev = 1 - _sum(prev7, 'late') / _sum(prev7, 'delivered') if _sum(prev7, 'delivered') else np.nan
sc_now = tail7['avg_score'].mean() if len(tail7) else np.nan
sc_prev = prev7['avg_score'].mean() if len(prev7) else np.nan

c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric('GMV（近7日）', f'R$ {gmv_now:,.0f}' if pd.notna(gmv_now) else '—',
          _delta(gmv_now, gmv_prev))
c2.metric('订单量（近7日）', f'{ord_now:,.0f}' if pd.notna(ord_now) else '—',
          _delta(ord_now, ord_prev))
c3.metric('客单价', f'R$ {aov_now:,.2f}' if pd.notna(aov_now) else '—',
          _delta(aov_now, aov_prev))
c4.metric('准时送达率', f'{ot_now*100:.2f}%' if pd.notna(ot_now) else '—',
          _delta(ot_now, ot_prev), delta_color='normal')
c5.metric('差评率', f'{bad_now*100:.2f}%' if pd.notna(bad_now) else '—',
          _delta(bad_now, bad_prev), delta_color='inverse')
c6.metric('平均评分', f'{sc_now:.2f}' if pd.notna(sc_now) else '—',
          _delta(sc_now, sc_prev, pct=False))

if len(tail7):
    st.caption(
        f'KPI 统计区间：**{tail7["dt"].min():%Y-%m-%d} ~ {tail7["dt"].max():%Y-%m-%d}**'
        f'（最近 7 个有订单日，共 {int(ord_now):,} 单）；'
        f'环比基准：{prev7["dt"].min():%Y-%m-%d} ~ {prev7["dt"].max():%Y-%m-%d}'
        if len(prev7) else
        f'KPI 统计区间：{tail7["dt"].min():%Y-%m-%d} ~ {tail7["dt"].max():%Y-%m-%d}'
    )

st.markdown('---')

# ============================================================================
# Tabs
# ============================================================================

tab1, tab2, tab3, tab4 = st.tabs(['📈 趋势', '🚚 履约健康', '⭐ 体验', '🚨 异常预警'])

# ── Tab 1：趋势 ─────────────────────────────────────────────────────────────

with tab1:
    fig = go.Figure()
    fig.add_trace(go.Bar(x=df['dt'], y=df['orders'], name='订单量',
                         marker_color='#5DADE2', opacity=.65))
    fig.add_trace(go.Scatter(x=df['dt'], y=df['orders_ma7'], name='订单量 7日均线',
                             line=dict(color='#1F618D', width=2)))
    fig.add_trace(go.Scatter(x=df['dt'], y=df['bad_rate_plot'] * 100,
                             name='差评率(%)', yaxis='y2',
                             line=dict(color='#C0392B', width=1.6)))
    fig.add_trace(go.Scatter(x=df['dt'], y=df['bad_rate_ma7'] * 100,
                             name='差评率 7日均线(%)', yaxis='y2',
                             line=dict(color='#7B241C', width=2, dash='dot')))
    fig.update_layout(
        height=460,
        title='订单量与差评率趋势',
        xaxis=dict(title=''),
        yaxis=dict(title='订单量'),
        yaxis2=dict(title='差评率(%)', overlaying='y', side='right', showgrid=False),
        legend=dict(orientation='h', y=1.08),
        hovermode='x unified',
        margin=dict(t=70, b=40),
    )
    st.plotly_chart(fig, width='stretch')

    st.caption('说明：无订单的日期订单量为 0、差评率为空（不画线），'
               '避免把「无数据」误读成「差评率归零」。')

# ── Tab 2：履约健康 ────────────────────────────────────────────────────────

with tab2:
    col_a, col_b = st.columns(2)

    with col_a:
        f1 = px.line(df, x='dt', y='on_time_plot',
                     title='准时送达率')
        f1.update_traces(line=dict(color='#27AE60', width=1.6))
        f1.update_layout(height=340, yaxis_tickformat='.0%',
                         xaxis_title='', yaxis_title='准时率',
                         yaxis_range=[0.7, 1.0])
        st.plotly_chart(f1, width='stretch')

    with col_b:
        f2 = px.line(df, x='dt', y='avg_prep', title='平均备货时长（天）')
        f2.update_traces(line=dict(color='#E8A33D', width=1.6))
        f2.update_layout(height=340, xaxis_title='', yaxis_title='天')
        st.plotly_chart(f2, width='stretch')

    col_c, col_d = st.columns(2)
    with col_c:
        f3 = px.line(df, x='dt', y='overdue_plot', title='超期发货率')
        f3.update_traces(line=dict(color='#C0392B', width=1.6))
        f3.update_layout(height=340, yaxis_tickformat='.0%',
                         xaxis_title='', yaxis_title='超期率')
        st.plotly_chart(f3, width='stretch')

    with col_d:
        st.markdown('#### 履约指标汇总')
        sub = df.dropna(subset=['delivered'])
        st.dataframe(
            pd.DataFrame({
                '指标': ['平均准时率', '加权超期发货率', '平均备货天数', '备货 P90（估）'],
                '值': [
                    f'{(1 - sub["late"].sum() / sub["delivered"].sum())*100:.2f}%',
                    f'{(df["overdue"].sum() / df["judgeable"].sum())*100:.2f}%',
                    f'{df["avg_prep"].mean():.2f} 天',
                    f'{df["avg_prep"].quantile(0.9):.2f} 天',
                ],
            }),
            hide_index=True, width='stretch',
        )
        st.caption('注：这里用「加权」口径（总分子/总分母），'
                   '不是日均值的平均——后者会被单量小的日子带偏。')

# ── Tab 3：体验 ────────────────────────────────────────────────────────────

with tab3:
    fig2 = go.Figure()
    fig2.add_trace(go.Scatter(x=df['dt'], y=df['bad_rate_plot'] * 100,
                              name='当日差评率', mode='lines',
                              line=dict(color='#E6B0AA', width=1)))
    fig2.add_trace(go.Scatter(x=df['dt'], y=df['bad_rate_ma7'] * 100,
                              name='7日均线', line=dict(color='#C0392B', width=2)))
    fig2.add_trace(go.Scatter(x=df['dt'], y=df['base28'] * 100,
                              name='28日基准', line=dict(color='#1E3A5F',
                                                      width=2, dash='dash')))
    fig2.update_layout(height=420, title='差评率：当日 vs 均线 vs 基准',
                       xaxis_title='', yaxis_title='差评率(%)',
                       legend=dict(orientation='h', y=1.1),
                       hovermode='x unified')
    st.plotly_chart(fig2, width='stretch')

    f4 = px.line(df, x='dt', y='score_plot', title='平均评分')
    f4.update_traces(line=dict(color='#2E86C1', width=1.6))
    f4.update_layout(height=320, xaxis_title='', yaxis_title='分',
                     yaxis_range=[3.5, 5])
    st.plotly_chart(f4, width='stretch')

# ── Tab 4：异常预警 ────────────────────────────────────────────────────────

with tab4:
    st.markdown('### 预警规则')
    st.markdown(f'''
- **方法**：二项分布 z-score，不是固定阈值
  `z = (当日差评率 - 28日加权基准) / sqrt(基准 × (1-基准) / 当日评价数)`
- **阈值**：`|z| ≥ {Z_ALERT}` 严重　`|z| ≥ {Z_WARN}` 关注　评价数 `< {MIN_SAMPLE}` 样本不足
- **依据**：实测 z 的标准差为 1.33（理论 1.0），说明业务波动超出纯抽样误差；
  若用 2σ 会告警 11.8% 的天数，故调至 3σ
''')

    cnt = df['alert'].value_counts()
    k1, k2, k3, k4 = st.columns(4)
    k1.metric('严重', int(cnt.get('严重', 0)))
    k2.metric('关注', int(cnt.get('关注', 0)))
    k3.metric('正常', int(cnt.get('正常', 0)))
    k4.metric('样本不足', int(cnt.get('样本不足', 0)))

    st.markdown('---')

    show = df[df['alert'].isin(['严重', '关注'])].copy()
    if show.empty:
        st.success('所选范围内没有异常日。')
    else:
        show = show.sort_values('bad_z', key=lambda s: s.abs(), ascending=False).head(30)
        show['日期'] = show['dt'].dt.strftime('%Y-%m-%d')
        show['差评率'] = (show['bad_review_rate'] * 100).round(2)
        show['基准'] = (show['base28'] * 100).round(2)
        show['z'] = show['bad_z'].round(2)

        # 一次性查出所有异常日的问题品类，再映射回来（不要逐行查库）
        cats = load_alert_categories(show['dt'].dt.date.tolist())
        if not cats.empty:
            hint = dict(zip(
                cats['dt'],
                cats['cat'] + '（贡献 ' + cats['bad'].astype(int).astype(str)
                + ' 条差评 / ' + cats['n'].astype(str) + ' 单）'
            ))
            show['下钻建议'] = show['dt'].dt.date.map(hint).fillna('—')
        else:
            show['下钻建议'] = '—'

        st.dataframe(
            show[['日期', 'alert', 'reviewed', '差评率', '基准', 'z', '下钻建议']]
            .rename(columns={'alert': '等级', 'reviewed': '评价数'}),
            hide_index=True, width='stretch',
            column_config={
                '差评率': st.column_config.NumberColumn(format='%.2f%%'),
                '基准': st.column_config.NumberColumn(format='%.2f%%'),
                'z': st.column_config.NumberColumn(format='%.2f'),
            },
        )
        st.caption('下钻建议 = 当天差评率最高的品类（样本≥10单），'
                   '用于指向下一步该看哪里。按 |z| 降序只显示前 30 条。')

# ============================================================================
# 维度下钻
# ============================================================================

st.markdown('---')
st.subheader('🔍 维度下钻')

level = st.radio('下钻维度', ['州', '品类'], horizontal=True)
drill = load_drill(start, end, level, sel_states or None, sel_cats or None)
drill = drill[drill['订单数'] >= 30]   # 过滤小样本，避免噪音

metric = st.selectbox('排序指标',
                      ['订单数', 'GMV', '客单价', '差评率', '迟到率', '平均备货天数'])
if metric not in drill.columns:          # 防御：列名大小写/别名不一致时回退
    metric = drill.columns[1]
drill = drill.sort_values(metric, ascending=False).head(20)

fd = px.bar(drill, x='维度', y=metric, text=metric,
            title=f'按{level}看 {metric}（Top 20，样本≥30单）')
fd.update_traces(marker_color='#2E86C1', textposition='outside')
fd.update_layout(height=420, xaxis_title='', margin=dict(t=60, b=80))
st.plotly_chart(fd, width='stretch')

st.dataframe(drill, hide_index=True, width='stretch')

st.markdown('---')
st.caption(
    'Olist 运营监控看板 · 指标口径见 metric_dictionary.md · '
    '异常检测用二项 z-score（非固定阈值）'
)
