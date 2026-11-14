"""Deterministic, portable Markdown reports from saved diagnostics."""

from __future__ import annotations


def _value(value, *, percent=False):
    if value is None:
        return "—"
    return f"{value:.2%}" if percent else f"{value:.4f}"


def evaluation_markdown(result: dict) -> str:
    meta, params = result["metadata"], result["metadata"]["evaluation"]
    universe = (
        f"动态条件股票池，区间行情覆盖 {meta['sample_size']} 只（每日按历史条件筛选）"
        if "universe_filters" in meta
        else f"历史动态成分池，区间相关股票并集 {meta['sample_size']} 只（不是每天全部参与排名）"
        if meta.get("membership_policy")
        else f"固定池 {meta['sample_size']} 只股票"
    )
    lines = [
        "# 因子评估报告",
        "",
        f"批次：`{meta['id']}`",
        f"数据版本：`{meta['data_version']}`",
        f"评分批次：`{meta['factor_run_id']}`",
        f"生成时间：{meta['created_at']}",
        f"入场日期：{meta['start']} 至 {meta['end']}；{universe}。",
        "",
        "## 计算口径",
        "",
        "评分日 T 收盘后生成信号，T+1 开盘入场，在 T+1+h 开盘离场。收益使用累计复权口径。",
        "离场日必须在评估结束日以内；缺失价格与尾部不足的标签不填充。标签只供事后评估，不进入策略。",
        f"最少横截面 {params['min_cross_section']} 只，分 {params['groups']} 组；方向统一为越高越好，波动率取负值。",
        "IC 是方向调整后原始因子与后续收益的 Pearson 相关；Rank IC 使用 Spearman 相关，由 Qlib 计算。",
        "IR 为每日 IC 均值除以样本标准差，不年化；常数截面及无法计算的统计显示为 —。",
        "",
        "## 因子设置",
        "",
        "| 实例 / 因子 | 权重 | 窗口（交易日） | 剔除最近 | 此前基准窗口 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for spec in meta["factor_config"]:
        lines.append(
            f"| {spec.get('id', spec['name'])} / {spec['name']} | {spec['weight']:.1%} | {spec['window'] or '—'} | {spec.get('skip', 0)} | {spec.get('baseline_window', '—')} |"
        )
    if meta.get("cross_validation"):
        lines += ["", "## 联合数据来源", "", "| 股票 | 核验结果 | 价格来源 | 复权来源 |", "| --- | --- | --- | --- |"]
        for stock in meta["cross_validation"]["stocks"]:
            status = {"verified": "核验通过", "fallback": "整段回退", "conflict": "存在冲突"}.get(
                stock["status"], stock["status"]
            )
            lines.append(f"| {stock['code']} | {status} | {stock['price_source']} | {stock['adjustment_source']} |")
        lines += [
            "",
            "估值、涨跌停与指数仍来自Tushare兼容服务；两源核验范围为未复权开高低收及复权收益。",
            "回退股票使用BaoStock后复权行情推导的相对因子，属于涨跌幅复权法；不等同于实际现金分红账本。",
            "不直接拼接两家绝对复权因子；源间冲突未解决时不发布。该核验不代表估值和财报已获独立认证。",
        ]
    if meta.get("temporal_audit"):
        lines += ["", "## 数据时点核验", "", "历史公开时点尚未完全认证；程序时序约束不等于供应商历史版本已认证。"]
        lines += [f"- {row['label']}：{row['detail']}" for row in meta["temporal_audit"]["items"]]
    lines += ["", "## 标签覆盖", "", "| 持有期 | 有效股票标签 | 排除标签 |", "| ---: | ---: | ---: |"]
    for horizon, labels in result["labels"].items():
        lines.append(f"| {horizon} | {labels['valid_observations']} | {labels['excluded_observations']} |")
    missing = sum(row["scored_instruments"] < params["min_cross_section"] for row in result["signal_coverage"])
    lines += [
        "",
        f"应评估入场日 {len(result['signal_coverage'])} 个，其中评分数量不足最低横截面的日期 {missing} 个。",
        "",
        "## IC 与分组收益",
        "",
        "| 因子 | 持有期 | 有效 IC 日期 | IC 均值 | Rank IC 均值 | Rank IC IR | 高组减低组平均收益 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, factor in result["factors"].items():
        for horizon, values in factor["horizons"].items():
            stats = values["summary"]
            lines.append(
                f"| {name} | {horizon} | {stats['rank_ic']['valid_dates']} | {_value(stats['ic']['mean'])} | "
                f"{_value(stats['rank_ic']['mean'])} | {_value(stats['rank_ic']['ir'])} | {_value(stats['mean_high_minus_low'], percent=True)} |"
            )
    lines += [
        "",
        "分组按平均秩百分位划分，Q1 最低、末组最高；相同因子值不按股票代码拆散，因此各组可能不等大或为空。",
        "高减低只统计两端组都存在的日期。各日期等权平均，多日标签可能重叠；这些收益没有连乘成净值。",
        "",
        "## 因子相关性",
        "",
        "| 因子一 | 因子二 | 平均每日 Rank 相关 | 有效日期 |",
        "| --- | --- | ---: | ---: |",
    ]
    for pair in result["correlations"]["pairs"]:
        lines.append(
            f"| {pair['first']} | {pair['second']} | {_value(pair['mean_rank_correlation'])} | {pair['valid_dates']} |"
        )
    lines += ["", "## 适用范围", ""]
    lines += [f"- {note}" for note in result["methodology"]["limitations"]]
    lines += [
        "- 每日估值的历史修订与实际发布时间尚未审计；本报告未接入财报因子。",
        "",
        "逐日IC、各组样本数及覆盖率见result.json；完整收益标签见JSON索引所列Parquet文件。"
        if result.get("tables")
        else "完整逐日 IC、各组样本数、覆盖率及逐股票收益标签见同目录 result.json。",
        "",
    ]
    return "\n".join(lines)
