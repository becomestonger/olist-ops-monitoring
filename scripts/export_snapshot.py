"""
从 PostgreSQL 导出看板所需的数据快照
=====================================
为什么需要：
    Streamlit Cloud 的服务器访问不到本机的 localhost:5432。
    所以把看板需要的字段导出成一个 parquet 文件一起提交，
    云端就只读文件、不连数据库。

用法：
    python scripts/export_snapshot.py

    # 自定义连接（默认连本机）
    set DATABASE_URL=postgresql+psycopg2://user:pwd@host:5432/analysis
    python scripts/export_snapshot.py

输出：
    data/snapshot.parquet   —— 约 2-3 MB，10 列 × 99,441 行
"""

import os
import sys

import pandas as pd
from sqlalchemy import create_engine, text

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, 'data', 'snapshot.parquet')

DB_URL = os.environ.get(
    'DATABASE_URL',
    'postgresql+psycopg2://postgres:123456@127.0.0.1:5432/analysis'
)

# 只导看板真正用到的 10 列（v_pbi_orders 有 30 列，全导会大一倍）
COLUMNS = [
    'purchase_date',
    'order_total',
    'is_delivered',
    'is_late',
    'is_overdue_ship',
    'review_score',
    'is_bad_review',
    'prep_days',
    'customer_state',
    'main_category',
]

SQL = f"""
    SELECT {', '.join(COLUMNS)}
    FROM olist.v_pbi_orders
    ORDER BY purchase_date
"""


def main():
    print(f'连接数据库: {DB_URL.split("@")[-1]}')
    engine = create_engine(DB_URL, pool_pre_ping=True)

    try:
        with engine.connect() as conn:
            df = pd.read_sql(text(SQL), conn)
    except Exception as exc:
        print(f'[失败] 无法连接数据库: {exc}', file=sys.stderr)
        print('      请确认 PostgreSQL 已启动，或设置 DATABASE_URL 环境变量', file=sys.stderr)
        sys.exit(1)

    # 类型收紧，减小文件体积
    df['purchase_date'] = pd.to_datetime(df['purchase_date']).dt.date

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    df.to_parquet(OUT, index=False, compression='snappy')

    size_mb = os.path.getsize(OUT) / 1024 / 1024
    print(f'导出完成: {len(df):,} 行 × {len(df.columns)} 列')
    print(f'输出文件: {OUT}')
    print(f'文件大小: {size_mb:.2f} MB')
    print(f'日期范围: {df["purchase_date"].min()} ~ {df["purchase_date"].max()}')

    # 快速自检
    reviewed = df['review_score'].notna().sum()
    bad = df['is_bad_review'].fillna(0).sum()
    print(f'自检: 已评价 {reviewed:,} 单，差评率 {bad / reviewed * 100:.2f}%  (应约 14.63%)')


if __name__ == '__main__':
    main()
