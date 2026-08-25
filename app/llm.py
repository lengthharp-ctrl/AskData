"""LLM 客户端：DeepSeek / Gemini / 任意 OpenAI 兼容接口，统一走 openai SDK。

切换模型只需改环境变量（ASKDATA_LLM_PROVIDER + ASKDATA_LLM_API_KEY），
mock 模式用于无 Key 演示与自动化测试。
"""

from __future__ import annotations

import re

from . import config
from .prompts import SYSTEM_PROMPT, build_user_prompt


class LLMError(RuntimeError):
    pass


def _extract_code(text: str) -> str:
    match = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    raise LLMError("模型没有返回代码块，请重试或换个问法")


def generate_code(question: str, schema: list[dict], row_count: int, preview: list[dict]) -> str:
    """根据问题 + 数据概览生成 pandas 代码。"""
    if config.LLM_PROVIDER == "mock":
        return _mock_code(question, schema)

    base_url, model = config.llm_settings()
    if not config.LLM_API_KEY or not base_url or not model:
        raise LLMError(
            "未配置 LLM：请设置 ASKDATA_LLM_PROVIDER / ASKDATA_LLM_API_KEY"
            "（DeepSeek 或 Gemini），或先用 mock 演示模式"
        )

    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=config.LLM_API_KEY,
            base_url=base_url,
            timeout=config.LLM_TIMEOUT_S,
        )
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(question, schema, row_count, preview)},
            ],
            temperature=config.LLM_TEMPERATURE,
            max_tokens=3000,
        )
        return _extract_code(resp.choices[0].message.content or "")
    except LLMError:
        raise
    except Exception as exc:
        raise LLMError(f"调用 {config.LLM_PROVIDER} 失败：{exc}") from exc


# ------------------------- mock 模式（演示/测试用） -------------------------

_FONT_SETUP = (
    'import matplotlib\n'
    'matplotlib.rcParams["font.sans-serif"] = '
    '["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "PingFang SC", "DejaVu Sans"]\n'
    'matplotlib.rcParams["axes.unicode_minus"] = False\n'
)


def _find_col(schema: list[dict], *keywords: str) -> str | None:
    for item in schema:
        name = str(item["name"])
        if any(k in name for k in keywords):
            return name
    return None


def _mock_code(question: str, schema: list[dict]) -> str:
    q = question
    date_col = _find_col(schema, "日期", "date") or "订单日期"
    amount_col = _find_col(schema, "销售额", "金额", "sales", "amount") or "销售额"
    profit_col = _find_col(schema, "利润", "profit") or "利润"
    category_col = _find_col(schema, "品类", "类别", "category") or "品类"
    region_col = _find_col(schema, "地区", "区域", "region") or "地区"
    channel_col = _find_col(schema, "渠道", "channel") or "渠道"

    if "月" in q and ("销售" in q or "金额" in q or "趋势" in q):
        return f"""{_FONT_SETUP}
# 按月统计销售额，画趋势折线图
df2 = df.copy()
df2["月份"] = pd.to_datetime(df2[{date_col!r}]).dt.to_period("M").astype(str)
monthly = df2.groupby("月份")[{amount_col!r}].sum().sort_index()
save_result(monthly, "按月销售额统计")
plt.figure(figsize=(9, 4.5))
plt.plot(monthly.index, monthly.values, marker="o", color="#4f46e5", linewidth=2)
plt.title("按月销售额趋势")
plt.xlabel("月份")
plt.ylabel("销售额")
plt.xticks(rotation=45)
plt.grid(alpha=0.3)
save_chart(name="monthly_sales")
"""
    if "月" in q and "利润" in q:
        return f"""{_FONT_SETUP}
# 按月统计利润，画趋势折线图
df2 = df.copy()
df2["月份"] = pd.to_datetime(df2[{date_col!r}]).dt.to_period("M").astype(str)
monthly = df2.groupby("月份")[{profit_col!r}].sum().sort_index()
save_result(monthly, "按月利润统计")
plt.figure(figsize=(9, 4.5))
plt.plot(monthly.index, monthly.values, marker="o", color="#10b981", linewidth=2)
plt.title("按月利润趋势")
plt.xlabel("月份")
plt.ylabel("利润")
plt.xticks(rotation=45)
plt.grid(alpha=0.3)
save_chart(name="monthly_profit")
"""
    if "利润" in q or "赚" in q:
        return f"""{_FONT_SETUP}
# 各品类利润对比柱状图
cat = df.groupby({category_col!r})[{profit_col!r}].sum().sort_values(ascending=False)
save_result(cat, "各品类利润")
plt.figure(figsize=(8, 4.5))
plt.bar(cat.index, cat.values, color=plt.cm.viridis(0.15 + 0.75 * np.linspace(0, 1, len(cat))))
plt.title("各品类利润对比")
plt.ylabel("利润")
plt.xticks(rotation=30)
for i, v in enumerate(cat.values):
    plt.text(i, v, f"{{v:,.0f}}", ha="center", va="bottom", fontsize=9)
save_chart(name="category_profit")
"""
    if "地区" in q or "占比" in q:
        return f"""{_FONT_SETUP}
# 各地区销售额占比（plotly 饼图）
import plotly.express as px
region = df.groupby({region_col!r})[{amount_col!r}].sum().reset_index()
region.columns = ["地区", "销售额"]
save_result(region.sort_values("销售额", ascending=False), "各地区销售额")
fig = px.pie(region, names="地区", values="销售额", title="各地区销售额占比", hole=0.35)
save_chart(fig, name="region_pie")
"""
    if "渠道" in q:
        return f"""{_FONT_SETUP}
# 各渠道销售额对比
ch = df.groupby({channel_col!r})[{amount_col!r}].sum().sort_values(ascending=False)
save_result(ch, "各渠道销售额")
plt.figure(figsize=(7, 4.2))
plt.barh(ch.index[::-1], ch.values[::-1], color="#10b981")
plt.title("各渠道销售额对比")
plt.xlabel("销售额")
for i, v in enumerate(ch.values[::-1]):
    plt.text(v, i, f"{{v:,.0f}}", va="center", fontsize=9)
save_chart(name="channel_sales")
"""
    return f"""{_FONT_SETUP}
# 数据概览：统计描述 + 前 10 行
save_result(df.describe(include="all"), "数据概览（describe）")
save_result(df.head(10), "数据前 10 行")
"""
