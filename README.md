# Olist 电商核心指标监控看板

> 面向数据运营的日常监控工具：**拉取指标 → 发现异动 → 下钻归因**。
> 不是静态报表，而是带统计化异常预警的交互看板。

<p>
  <img src="https://img.shields.io/badge/Streamlit-1.66-FF4B4B?logo=streamlit&logoColor=white" alt="Streamlit">
  <img src="https://img.shields.io/badge/PostgreSQL-18-336791?logo=postgresql&logoColor=white" alt="PostgreSQL">
  <img src="https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Data-99%2C441%20orders-informational" alt="Data">
</p>

**🔗 在线体验**：<部署后填入 Streamlit Cloud 链接>

<!--
  部署完成后把上面这行改成：
  **🔗 在线体验**：[olist-ops-monitor.streamlit.app](https://xxx.streamlit.app)
  链接会出现在 GitHub 仓库首页，HR 点开即可使用，无需安装任何环境。
-->

---

## 这个看板解决什么问题

数据运营每天真正做的事不是"画图"，而是三件事：

| 环节 | 传统做法 | 本看板 |
|---|---|---|
| **拉取指标** | 每天手动跑 SQL、贴到 Excel | 六个核心 KPI 自动汇总，带 7 日环比 |
| **发现异动** | 靠人盯着看，或用固定阈值（如"差评率 > 15% 报警"）| **二项分布 z-score** 自动判定，带三级分级 |
| **下钻归因** | 再写几条 SQL 逐层查 | 大盘 → 州 → 品类 → 卖家，交互下钻 |

> 固定阈值的问题：阈值紧贴基线会天天报警，不考虑样本量会把"5 单 2 个差评"当成异常。
> 详见 [metric_dictionary.md](metric_dictionary.md) 的推算过程。

---

## 核心功能

### 1. 六个核心 KPI（带环比）

GMV / 订单量 / 客单价 / 准时送达率 / 差评率 / 平均评分

用**近 7 日 vs 前 7 日**对比，避免单日噪音；差评率环比用反向配色（下降是好事）。

### 2. 四个分析维度

| Tab | 内容 |
|---|---|
| 📈 趋势 | 订单量（柱）+ 差评率（线）双轴，叠加 7 日均线 |
| 🚚 履约健康 | 准时率 / 备货时长 / 超期发货率 |
| ⭐ 体验 | 差评率三线对比：当日 vs 7 日均线 vs 28 日基准 |
| 🚨 异常预警 | 等级计数 + 异常日明细（z 值 + 下钻建议）|

### 3. 异常预警：二项分布 z-score

```
基准比率 p₀ = 近 28 天总差评数 / 近 28 天总评价数     ← 加权，不是日均值的平均
标准误  SE  = sqrt( p₀ × (1 − p₀) / 当日评价数 )
z            = (当日差评率 − p₀) / SE
```

| 等级 | 判定 |
|---|---|
| 严重 | \|z\| ≥ 3.0 |
| 关注 | \|z\| ≥ 2.5 |
| 正常 | 其他 |
| 样本不足 | 当日评价数 < 30，只显示不告警 |

**为什么是 3.0 而不是 2.0**：实测 z 的标准差为 1.33（理论应为 1.0），说明业务波动
超出纯抽样误差（周末、大促、物流事件）。用 2.0σ 会告警 11.8% 的天数（约每月 3–4 天），
运营会直接忽略；调到 3.0σ 后告警率降至 6.9%（约每月 2 天），且黑五、2018-02 运输异常
等真实事件仍能全部捕获。

**修正效果对比**：

| 日期 | 评价数 | 差评率 | 简单 z | 二项 z | 判定 |
|---|---:|---:|---:|---:|---|
| 2018-08-29 | 14 | 21.4% | 2.68 ⚠️误报 | 1.29 | 样本不足 |
| 2018-05-23 | 132 | 7.6% | -2.18 ⚠️误报 | -1.66 | 正常 |

8 个被简单 z 误报的日子，修正后全部回归正常。

### 4. 预警命中的真实事件

| 日期 | 评价数 | 差评率 | z | 对应事件 |
|---|---:|---:|---:|---|
| 2017-11-24 | 1,166 | 22.56% | 5.00 | 黑五 |
| 2018-02-26 | 294 | 30.61% | 4.77 | 2018-02 运输异常 |
| 2017-11-25 | 493 | 25.15% | 4.27 | 黑五 |

> 这三个异常日与另一份独立分析（[差评归因报告](https://github.com/becomestonger/olist-review-attribution)）
> 发现的异常期完全吻合——说明预警规则有效，不是调参凑出来的。

---

## 指标口径

8 个指标的完整定义（业务定义 / 计算公式 / 口径边界 / 数据来源 / 更新频率 / 责任人 / 异常处理）
见 **[metric_dictionary.md](metric_dictionary.md)**。

里面记录了做分析时踩过的两个坑：

- 用 `INNER JOIN` 取订单，丢掉 775 笔无商品明细的订单，差评率算成 14.13%（正确 14.63%）
- 一个 `review_id` 可能对应多个 `order_id`，直接 join 会让一单变多行

---

## 数据来源说明

看板有**两种运行模式**，代码自动切换：

| 环境 | 数据来源 | 说明 |
|---|---|---|
| 本地 | PostgreSQL `olist.v_pbi_orders` | 连得上数据库就读库（实时） |
| 云端 | `data/snapshot.parquet` | Streamlit Cloud 访问不到本机数据库，改读快照 |

快照只有 **0.70 MB**（99,441 行 × 10 列），随仓库提交。
两种模式的聚合结果一致（差评率 14.63%、迟到率 8.11%、GMV R$ 15,843,553.24）。

重新生成快照：

```bash
python scripts/export_snapshot.py
```

> 侧边栏底部会显示当前用的是哪个数据源。

---

## 项目结构

```
olist-ops-monitoring/
├── app.py                      # Streamlit 看板主程序
├── metric_dictionary.md        # 8 个指标的口径定义
├── requirements.txt
├── 启动看板.bat                 # Windows 一键启动（双击）
├── data/
│   └── snapshot.parquet        # 云端数据快照（0.70 MB）
├── scripts/
│   └── export_snapshot.py      # 从数据库导出快照
└── sql/
    └── 08_daily_ops_metrics.sql # 日级指标视图 + 多粒度汇总视图
```

---

## 本地运行

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 建数据库视图（需先有 Olist 数据，见另一仓库）
psql -h 127.0.0.1 -U postgres -d analysis -f sql/08_daily_ops_metrics.sql

# 3. 启动
streamlit run app.py
```

浏览器打开 http://localhost:8501 。

**Windows 用户**：直接双击 `启动看板.bat`，会自动检查 PostgreSQL 并启动服务。

### 连接配置

默认连 `localhost:5432`。想改连接，三种方式（优先级从高到低）：

1. `.streamlit/secrets.toml` 里写 `DATABASE_URL = "..."`（**不要提交到 Git**）
2. 环境变量 `DATABASE_URL`
3. 代码里的默认值

---

## 建库 SQL

`sql/08_daily_ops_metrics.sql` 建立了两个视图：

| 视图 | 行数 | 用途 |
|---|---:|---|
| `v_daily_ops_metrics` | 774 | 日级指标（含环比、移动平均、滚动 z-score、预警等级）|
| `v_metrics_by_dim` | 1,480 | 多粒度汇总（大盘 / 州 / 品类 / 州×品类）|

其中用到了 5 个 PostgreSQL 进阶特性：

- `generate_series` —— 补齐 140 天无订单的日期缺口
- `LAG` —— 日环比
- `ROWS BETWEEN` —— 7 日 / 28 日移动窗口
- `percentile_cont` —— 备货时长 P50/P90/P99
- `GROUPING SETS` —— 一次算出 4 个粒度

---

## 数据来源与许可

数据来自 [Olist 巴西电商公开数据集](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)，CC BY-NC-SA 4.0。

仓库中的 parquet 为**聚合所需字段的派生快照**，不含原始 CSV，不含客户个人信息
（`customer_unique_id` 已公开脱敏）。

---

## 相关项目

- [Olist 客户差评归因分析](https://github.com/becomestonger/olist-review-attribution) ——
  用统计建模回答"为什么会有差评"，本看板负责"持续监控"与"验证改进"
