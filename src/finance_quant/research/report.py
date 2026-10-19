"""Deterministic, portable Markdown reports from saved diagnostics."""

from __future__ import annotations


def _value(value, *, percent=False):
    if value is None:
        return "—"
    return f"{value:.2%}" if percent else f"{value:.4f}"


def evaluation_markdown(result: dict) -> str:
    meta, params = result["metadata"], result["metadata"]["evaluation"]
    lines = ["# 因子评估报告", "", f"批次：`{meta['id']}`", f"数据版本：`{meta['data_version']}`",
             f"评分批次：`{meta['factor_run_id']}`", f"生成时间：{meta['created_at']}",
             f"入场日期：{meta['start']} 至 {meta['end']}；固定池 {meta['sample_size']} 只股票。", "",
             "## 计算口径", "", "评分日 T 收盘后生成信号，T+1 开盘入场，在 T+1+h 开盘离场。收益使用累计复权口径。",
             "离场日必须在评估结束日以内；缺失价格与尾部不足的标签不填充。标签只供事后评估，不进入策略。",
             f"最少横截面 {params['min_cross_section']} 只，分 {params['groups']} 组；方向统一为越高越好，波动率取负值。",
             "IC 是方向调整后原始因子与后续收益的 Pearson 相关；Rank IC 使用 Spearman 相关，由 Qlib 计算。",
             "IR 为每日 IC 均值除以样本标准差，不年化；常数截面及无法计算的统计显示为 —。", "",
             "## 因子设置", "", "| 因子 | 权重 | 窗口（交易日） |", "| --- | ---: | ---: |"]
    for spec in meta["factor_config"]:
        lines.append(f"| {spec['name']} | {spec['weight']:.1%} | {spec['window'] or '—'} |")
    lines += ["", "## 标签覆盖", "", "| 持有期 | 有效股票标签 | 排除标签 |", "| ---: | ---: | ---: |"]
    for horizon, labels in result["labels"].items():
        lines.append(f"| {horizon} | {labels['valid_observations']} | {labels['excluded_observations']} |")
    missing = sum(row["scored_instruments"] < params["min_cross_section"] for row in result["signal_coverage"])
    lines += ["", f"应评估入场日 {len(result['signal_coverage'])} 个，其中评分数量不足最低横截面的日期 {missing} 个。", "",
              "## IC 与分组收益", "", "| 因子 | 持有期 | 有效 IC 日期 | IC 均值 | Rank IC 均值 | Rank IC IR | 高组减低组平均收益 |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for name, factor in result["factors"].items():
        for horizon, values in factor["horizons"].items():
            stats = values["summary"]
            lines.append(f"| {name} | {horizon} | {stats['rank_ic']['valid_dates']} | {_value(stats['ic']['mean'])} | "
                         f"{_value(stats['rank_ic']['mean'])} | {_value(stats['rank_ic']['ir'])} | {_value(stats['mean_high_minus_low'], percent=True)} |")
    lines += ["", "分组按平均秩百分位划分，Q1 最低、末组最高；相同因子值不按股票代码拆散，因此各组可能不等大或为空。",
              "高减低只统计两端组都存在的日期。各日期等权平均，多日标签可能重叠；这些收益没有连乘成净值。", "",
              "## 因子相关性", "", "| 因子一 | 因子二 | 平均每日 Rank 相关 | 有效日期 |", "| --- | --- | ---: | ---: |"]
    for pair in result["correlations"]["pairs"]:
        lines.append(f"| {pair['first']} | {pair['second']} | {_value(pair['mean_rank_correlation'])} | {pair['valid_dates']} |")
    lines += ["", "## 适用范围", ""]
    lines += [f"- {note}" for note in result["methodology"]["limitations"]]
    lines += ["- 每日估值的历史修订与实际发布时间尚未审计；本报告未接入财报因子。",
              "", "完整逐日 IC、各组样本数、覆盖率及逐股票收益标签见同目录 result.json。", ""]
    return "\n".join(lines)
