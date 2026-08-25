"""示例数据：电商销售数据（确定性随机，约 800 行），供「一键体验」。"""

from __future__ import annotations

import numpy as np
import pandas as pd

CATEGORIES = {
    "电子产品": [("无线蓝牙耳机", 299), ("智能手表", 1299), ("便携充电宝", 129), ("机械键盘", 499)],
    "服装鞋帽": [("纯棉T恤", 79), ("运动鞋", 399), ("轻薄羽绒服", 899), ("牛仔裤", 259)],
    "食品饮料": [("精品咖啡豆", 108), ("坚果礼盒", 158), ("进口红酒", 268), ("有机牛奶", 59)],
    "家居日用": [("水洗棉四件套", 329), ("落地灯", 199), ("收纳箱", 45), ("香薰蜡烛", 69)],
    "美妆个护": [("保湿精华", 359), ("口红", 219), ("氨基酸洗面奶", 89), ("淡香水", 499)],
}
REGIONS = ["华东", "华南", "华北", "西南", "华中", "东北"]
CHANNELS = ["线上商城", "直播带货", "小程序", "线下门店"]
CUSTOMERS = ["新客", "老客", "会员"]


def generate_sales_data(rows: int = 800, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    hours = pd.date_range("2025-01-01", "2025-12-31 23:00", freq="h")
    records = []
    for i in range(rows):
        category = str(rng.choice(list(CATEGORIES)))
        product, price = CATEGORIES[category][int(rng.integers(0, len(CATEGORIES[category])))]
        qty = int(rng.integers(1, 6))
        amount = round(qty * price, 2)
        cost = round(amount * float(rng.uniform(0.55, 0.82)), 2)
        date = hours[int(rng.integers(0, len(hours)))]
        records.append(
            {
                "订单日期": date,
                "品类": category,
                "商品名称": product,
                "数量": qty,
                "单价": price,
                "销售额": amount,
                "成本": cost,
                "利润": round(amount - cost, 2),
                "地区": str(rng.choice(REGIONS)),
                "渠道": str(rng.choice(CHANNELS)),
                "客户类型": str(rng.choice(CUSTOMERS)),
            }
        )
    df = pd.DataFrame(records).sort_values("订单日期").reset_index(drop=True)
    df["订单号"] = [f"SO-{d:%Y%m%d}-{i+1:04d}" for i, d in enumerate(df["订单日期"])]
    df = df[["订单号", "订单日期", "品类", "商品名称", "数量", "单价", "销售额", "成本", "利润", "地区", "渠道", "客户类型"]]
    return df


def save_sample_csv(path) -> None:
    df = generate_sales_data()
    df.to_csv(path, index=False, encoding="utf-8-sig")

