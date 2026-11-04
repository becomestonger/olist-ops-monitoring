-- ============================================================================
-- 项目二 · 阶段 1：日级运营指标视图
--
-- 目的：给监控看板提供一个「一行一天」的指标源，
--       看板只需 SELECT * WHERE metric_date BETWEEN ... 即可。
--
-- 产出：
--   olist.v_daily_ops_metrics   日级指标（774 天，含 140 天无订单）
--   olist.v_metrics_by_dim      多粒度汇总（大盘 / 州 / 品类 / 州×品类）
--
-- 设计要点：
--   ① generate_series 补齐无订单的日期（774 天里 140 天没订单）
--   ② 比率类在分母为 0 时返回 NULL 而不是 0（否则曲线会假性跌到 0）
--   ③ 保留原始分子分母，方便看板按不同口径重新加权
--   ④ 异常预警用 28 日滚动 z-score，不用硬编码阈值
--
-- 运行：
--   psql -h 127.0.0.1 -U postgres -d analysis -f sql/08_daily_ops_metrics.sql
-- ============================================================================

DROP VIEW IF EXISTS olist.v_daily_ops_metrics;

CREATE VIEW olist.v_daily_ops_metrics AS
WITH cal AS (
    -- ① 日期骨架：2016-09-04 ~ 2018-10-17，共 774 天，一天不少
    SELECT d::date AS metric_date
    FROM generate_series('2016-09-04'::date, '2018-10-17'::date, '1 day') AS d
),
daily AS (
    -- ② 日级聚合：只含有订单的天（634 天）
    SELECT purchase_date AS metric_date,

           -- ── 核心大盘 ──
           sum(order_total)                                        AS gmv,
           count(*)                                                AS orders,
           avg(order_total)                                        AS aov,

           -- ── 履约健康度（分子分母都留，便于重新加权）──
           count(*) FILTER (WHERE is_delivered = 1)                AS delivered_orders,
           count(*) FILTER (WHERE is_late = 1)                     AS late_orders,
           count(*) FILTER (WHERE is_overdue_ship IS NOT NULL)     AS judgeable_ship,
           count(*) FILTER (WHERE is_overdue_ship = 1)             AS overdue_orders,
           avg(prep_days)                                          AS avg_prep_days,
           percentile_cont(0.5)  WITHIN GROUP (ORDER BY prep_days) AS prep_p50,
           percentile_cont(0.9)  WITHIN GROUP (ORDER BY prep_days) AS prep_p90,
           percentile_cont(0.99) WITHIN GROUP (ORDER BY prep_days) AS prep_p99,

           -- ── 体验指标 ──
           count(review_score)                                     AS reviewed_orders,
           count(*) FILTER (WHERE is_bad_review = 1)               AS bad_orders,
           avg(review_score)                                       AS avg_score

    FROM olist.v_pbi_orders
    GROUP BY purchase_date
),
joined AS (
    -- ③ 骨架 LEFT JOIN 事实：补齐 140 天缺口
    --    计数类补 0（那天真的 0 单）；比率类留到下一步按分母判断
    SELECT c.metric_date,
           COALESCE(d.gmv, 0)              AS gmv,
           COALESCE(d.orders, 0)           AS orders,
           d.aov,
           COALESCE(d.delivered_orders, 0) AS delivered_orders,
           COALESCE(d.late_orders, 0)      AS late_orders,
           COALESCE(d.judgeable_ship, 0)   AS judgeable_ship,
           COALESCE(d.overdue_orders, 0)   AS overdue_orders,
           d.avg_prep_days, d.prep_p50, d.prep_p90, d.prep_p99,
           COALESCE(d.reviewed_orders, 0)  AS reviewed_orders,
           COALESCE(d.bad_orders, 0)       AS bad_orders,
           d.avg_score,
           (d.metric_date IS NOT NULL)     AS has_orders
    FROM cal c
    LEFT JOIN daily d ON d.metric_date = c.metric_date
),
rates AS (
    -- ④ 计算比率：分母为 0 时返回 NULL，绝不返回 0
    SELECT metric_date, has_orders,
           gmv, orders, aov,
           delivered_orders, late_orders, reviewed_orders, bad_orders,
           judgeable_ship, overdue_orders,

           -- 履约健康度
           CASE WHEN delivered_orders > 0
                THEN round((1.0 - 1.0 * late_orders / delivered_orders)::numeric, 4)
           END AS on_time_rate,

           avg_prep_days, prep_p50, prep_p90, prep_p99,

           CASE WHEN judgeable_ship > 0
                THEN round((1.0 * overdue_orders / judgeable_ship)::numeric, 4)
           END AS overdue_ship_rate,

           -- 体验
           CASE WHEN reviewed_orders > 0
                THEN round((1.0 * bad_orders / reviewed_orders)::numeric, 4)
           END AS bad_review_rate,

           avg_score
    FROM joined
),
windowed AS (
    -- ⑤⑥⑦ 所有窗口计算集中在这里
    SELECT r.*,

           -- ── 环比（和前一天比）──
           LAG(orders) OVER w                                      AS orders_prev_day,
           round((100.0 * (orders - LAG(orders) OVER w)
                  / nullif(LAG(orders) OVER w, 0))::numeric, 1)    AS orders_dod_pct,
           round((100.0 * (gmv - LAG(gmv) OVER w)
                  / nullif(LAG(gmv) OVER w, 0))::numeric, 1)       AS gmv_dod_pct,

           -- ── 移动平均（消除单日噪音）──
           round(avg(bad_review_rate) OVER w7,  4)                 AS bad_rate_ma7,
           round(avg(bad_review_rate) OVER w28, 4)                 AS bad_rate_ma28,
           round(avg(on_time_rate)    OVER w28, 4)                 AS on_time_ma28,

           -- ── 28 日加权基准（用总分子/总分母，不是日均值的平均）──
           round((1.0 * sum(bad_orders) OVER w28
                  / nullif(sum(reviewed_orders) OVER w28, 0))::numeric, 4) AS bad_rate_base28,

           -- ── 简单滚动标准差（保留作对照，正式预警不用它）──
           round(stddev_samp(bad_review_rate) OVER w28, 4)         AS bad_rate_std28

    FROM rates r
    WINDOW
        w   AS (ORDER BY metric_date),
        w7  AS (ORDER BY metric_date ROWS BETWEEN 6  PRECEDING AND CURRENT ROW),
        w28 AS (ORDER BY metric_date ROWS BETWEEN 27 PRECEDING AND CURRENT ROW)
)
SELECT
    metric_date, has_orders,
    gmv, orders, aov,
    on_time_rate,
    avg_prep_days, prep_p50, prep_p90, prep_p99,
    overdue_ship_rate,
    bad_review_rate,
    avg_score,
    -- 原始分子分母（供看板按不同口径重算）
    delivered_orders, late_orders, reviewed_orders, bad_orders,
    judgeable_ship, overdue_orders,

    orders_prev_day, orders_dod_pct, gmv_dod_pct,
    bad_rate_ma7, bad_rate_ma28, on_time_ma28,
    bad_rate_base28,
    bad_rate_std28,

    -- ── ⑨ 对照用：简单 z-score（不修正样本量，会误报小样本日）──────────────
    round(((bad_review_rate - bad_rate_ma28)
           / nullif(bad_rate_std28, 0))::numeric, 2)              AS bad_rate_z_naive,

    -- ── ⑩ 正式用：二项分布 z-score（自动修正样本量）────────────────────────
    --    z = (当日比率 - 基准比率) / sqrt(基准*(1-基准)/当日样本量)
    --    样本量小 -> 标准误大 -> z 小 -> 不会误报
    round(((bad_review_rate - bad_rate_base28)
           / nullif(sqrt(bad_rate_base28 * (1 - bad_rate_base28)
                         / nullif(reviewed_orders, 0)), 0))::numeric, 2) AS bad_rate_z,

    -- ── ⑪ 预警等级（样本量下限 + 二项 z）───────────────────────────────────
    --    阈值设定依据（实测）：
    --      · z 的标准差为 1.33（理论应为 1.0），说明业务波动超出纯抽样误差
    --      · 若用 2.0σ，告警率 11.8%（约每月 3-4 天）→ 会造成告警疲劳
    --      · 改用 3.0σ/2.5σ 后告警率 6.9%（约每月 2 天），且黑五、2018-02 仍能捕获
    CASE
        WHEN bad_review_rate IS NULL
          OR reviewed_orders < 30 THEN '样本不足'
        WHEN abs((bad_review_rate - bad_rate_base28)
                 / nullif(sqrt(bad_rate_base28 * (1 - bad_rate_base28)
                               / nullif(reviewed_orders, 0)), 0)) >= 3.0   THEN '严重'
        WHEN abs((bad_review_rate - bad_rate_base28)
                 / nullif(sqrt(bad_rate_base28 * (1 - bad_rate_base28)
                               / nullif(reviewed_orders, 0)), 0)) >= 2.5 THEN '关注'
        ELSE '正常'
    END AS bad_rate_alert

FROM windowed;

COMMENT ON VIEW olist.v_daily_ops_metrics IS
    '日级运营指标：774 天（含 140 天无订单），含环比/移动平均/滚动 z-score 预警';


-- ============================================================================
-- 多粒度汇总视图：一次算出 大盘 / 州 / 品类 / 州×品类 四个粒度
-- 看板下钻时不需要写 4 条 SQL
-- ============================================================================

DROP VIEW IF EXISTS olist.v_metrics_by_dim;

CREATE VIEW olist.v_metrics_by_dim AS
SELECT CASE GROUPING(customer_state, main_category)
            WHEN 0 THEN '州×品类'
            WHEN 1 THEN '仅州'
            WHEN 2 THEN '仅品类'
            WHEN 3 THEN '大盘'
       END                                                        AS level,
       customer_state,
       main_category,
       count(*)                                                   AS orders,
       round(sum(order_total)::numeric, 2)                        AS gmv,
       round((100.0 * count(*) FILTER (WHERE is_bad_review = 1)
              / nullif(count(review_score), 0))::numeric, 2)      AS bad_review_rate,
       round((100.0 * count(*) FILTER (WHERE is_late = 1)
              / nullif(count(*) FILTER (WHERE is_delivered = 1), 0))::numeric, 2) AS late_rate,
       round((100.0 * count(*) FILTER (WHERE is_overdue_ship = 1)
              / nullif(count(*) FILTER (WHERE is_overdue_ship IS NOT NULL), 0))::numeric, 2) AS overdue_ship_rate,
       round(avg(prep_days)::numeric, 2)                          AS avg_prep_days
FROM olist.v_pbi_orders
GROUP BY GROUPING SETS (
    (customer_state, main_category),
    (customer_state),
    (main_category),
    ()
);

COMMENT ON VIEW olist.v_metrics_by_dim IS
    '多粒度运营指标：大盘/州/品类/州×品类 四个粒度，供看板下钻';


-- ============================================================================
-- 验证区（跑完看这 6 条结果是否对）
-- ============================================================================

-- ① 应该 774 行，其中 140 行无订单
SELECT count(*)                                                AS 总天数,
       count(*) FILTER (WHERE has_orders)                      AS 有订单天数,
       count(*) FILTER (WHERE NOT has_orders)                  AS 无订单天数
FROM olist.v_daily_ops_metrics;

-- ② 前 3 个有数据的天，看字段是否正常
SELECT metric_date, orders, gmv, on_time_rate, bad_review_rate, avg_score,
       bad_rate_ma28, bad_rate_z, bad_rate_alert
FROM olist.v_daily_ops_metrics
WHERE bad_review_rate IS NOT NULL
ORDER BY metric_date
LIMIT 3;

-- ③ 整体核对：和项目一的基线对齐（差评率应约 14.63%，迟到率约 8.11%）
SELECT round((100.0 * sum(bad_orders) / nullif(sum(reviewed_orders), 0))::numeric, 2)   AS 差评率,
       round((100.0 * sum(late_orders) / nullif(sum(delivered_orders), 0))::numeric, 2) AS 迟到率,
       round((100.0 * sum(overdue_orders) / nullif(sum(judgeable_ship), 0))::numeric, 2) AS 超期发货率,
       sum(orders)                                                                     AS 订单合计,
       round(sum(gmv)::numeric, 2)                                                     AS GMV合计
FROM olist.v_daily_ops_metrics;

-- ④ 预警等级分布（应该大部分是"正常"）
SELECT bad_rate_alert, count(*) AS 天数
FROM olist.v_daily_ops_metrics
GROUP BY 1 ORDER BY 2 DESC;

-- ⑤ |z| > 2 的异常日（排除样本不足日），应该能看到 2017-11 / 2018-03 附近
SELECT metric_date, reviewed_orders AS 样本, round(bad_review_rate::numeric, 4) AS 差评率,
       bad_rate_base28, bad_rate_z, bad_rate_alert
FROM olist.v_daily_ops_metrics
WHERE bad_rate_z IS NOT NULL AND abs(bad_rate_z) > 2
  AND bad_rate_alert <> '样本不足'
ORDER BY abs(bad_rate_z) DESC
LIMIT 10;

-- ⑤-b 修正前后对比：简单 z 会把小样本日误报，二项 z 不会
SELECT metric_date, reviewed_orders AS 样本, round(bad_review_rate::numeric, 4) AS 差评率,
       bad_rate_z_naive AS 简单z, bad_rate_z AS 二项z, bad_rate_alert
FROM olist.v_daily_ops_metrics
WHERE bad_rate_z_naive IS NOT NULL
  AND abs(bad_rate_z_naive) > 2
  AND abs(coalesce(bad_rate_z, 0)) < 2
ORDER BY abs(bad_rate_z_naive) DESC
LIMIT 8;

-- ⑥ 多粒度视图：每个 level 的订单合计都应是 99,441
SELECT level, count(*) AS 行数, sum(orders) AS 订单合计
FROM olist.v_metrics_by_dim
GROUP BY level ORDER BY 1;
