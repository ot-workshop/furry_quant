"""Explicit commands: remote acquisition, offline research, read-only serving."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path


def main() -> None:
    for variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(variable, "1")
    parser = argparse.ArgumentParser(description="量化研究工作台（Qlib 后端）")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="运行合成样本，不联网")
    demo.add_argument("--runs-dir", type=Path, default=Path("runs"))
    demo.add_argument("--data-dir", type=Path, default=Path("data/sample-qlib"))
    commands.add_parser("probe", help="验证Tushare服务能力，不回显密钥")
    for name, help_text in [("download", "分片下载并校验真实行情，失败后重跑可续传"),
                            ("plan", "离线查看下载配置和请求数量上限"),
                            ("status", "离线查看数据覆盖和下载记录")]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--config", type=Path)
        command.add_argument("--data-dir", type=Path)
        if name != "status":
            command.add_argument("--start")
            command.add_argument("--end")
            command.add_argument("--codes", nargs="+")
            command.add_argument("--benchmark")
        if name == "download":
            command.add_argument("--refresh", action="store_true", help="重新请求全部分片，保留旧快照")
    real = commands.add_parser("real", help="从本地冻结快照计算因子并运行Qlib，不联网")
    real.add_argument("--config", type=Path)
    real.add_argument("--data-dir", type=Path)
    real.add_argument("--runs-dir", type=Path)
    for field in ["start", "end"]:
        real.add_argument(f"--{field}")
    for field in ["topk", "n-drop"]:
        real.add_argument(f"--{field}", type=int)
    for field in ["initial-cash", "risk-degree", "open-cost", "close-cost", "min-cost"]:
        real.add_argument(f"--{field}", type=float)
    evaluate = commands.add_parser("evaluate", help="离线评估因子IC、分组收益与相关性，保存JSON和报告")
    evaluate.add_argument("--config", type=Path, required=True)
    serve = commands.add_parser("serve", help="启动本机只读API和前端")
    serve.add_argument("--config", type=Path)
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--runs-dir", type=Path)
    serve.add_argument("--data-dir", type=Path)
    args = parser.parse_args()

    def output(result):
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))

    try:
        from finance_quant.configuration import DataConfig, BacktestConfig, load_config
        config = load_config(args.config) if getattr(args, "config", None) else None
        if config:
            overrides = ["data_dir", "runs_dir", "start", "end", "codes", "benchmark", "topk", "n_drop",
                         "initial_cash", "risk_degree", "open_cost", "close_cost", "min_cost"]
            if any(getattr(args, field, None) is not None for field in overrides):
                parser.error("使用 --config 时请在配置文件内修改参数，不要混用同类命令行覆盖项")
        if args.command == "demo":
            from finance_quant.integrations.qlib_demo import run_demo
            output(run_demo(args.runs_dir, args.data_dir))
        elif args.command == "probe":
            from finance_quant.data.probe import probe_service
            probe_service()
        elif args.command in {"download", "plan", "status"}:
            from finance_quant.data.catalog import catalog_status
            if args.command == "status":
                output(catalog_status(config.data.root if config else args.data_dir or Path("data/managed")))
                return
            from finance_quant.data.managed import download_dataset, month_ranges, year_ranges
            from finance_quant.data.market import DEFAULT_CODES
            data = config.data if config else DataConfig(args.data_dir or Path("data/managed"),
                args.start or "20260803", args.end or "20260930", tuple(args.codes or DEFAULT_CODES), args.benchmark or "000300.SH")
            if args.command == "plan":
                import pandas as pd
                partitions = len(year_ranges(data.start, data.end))
                calendar_end = (pd.Timestamp(data.end) + pd.Timedelta(days=20)).strftime("%Y%m%d")
                output({"data": {**asdict(data), "root": str(data.root)},
                        "max_requests_without_cache": len(month_ranges(data.start, calendar_end)) + len(data.codes) * (1 + 4 * partitions) + partitions,
                        "note": "仅预览，不联网；请求上限不含缺失记录补抓与重试。行情按年分片、按月校验；已完成分片复用，边界年份变化需重新获取。固定池并非历史成分。"})
            else:
                result = download_dataset(data, refresh=args.refresh)
                manifest = result["manifest"]
                output({"job_id": result["job_id"], **{key: manifest[key] for key in ["id", "rows", "trading_days", "codes", "benchmark"]}})
        elif args.command == "real":
            from finance_quant.application.real_workflow import run_real, run_configured
            if config:
                result = run_configured(config)
                from finance_quant.data.managed import atomic_json
                atomic_json(config.runs_dir / "active-research.json", config.as_dict())
                output(result)
            else:
                defaults = BacktestConfig(args.start or "2026-09-01", args.end or "2026-09-30",
                    **{key: getattr(args, key) for key in ["initial_cash", "topk", "n_drop", "risk_degree", "open_cost", "close_cost", "min_cost"]
                       if getattr(args, key) is not None})
                output(run_real(args.data_dir or Path("data/tushare/20260803-20260930"), args.runs_dir or Path("runs"), **asdict(defaults)))
        elif args.command == "evaluate":
            from finance_quant.application.evaluation_workflow import evaluate_configured
            output(evaluate_configured(config))
        elif args.command == "serve":
            import uvicorn
            from finance_quant.api import create_app
            uvicorn.run(create_app(config.runs_dir if config else args.runs_dir,
                                  data_root=config.data.root if config else args.data_dir), host="127.0.0.1", port=args.port)
    except (ValueError, FileNotFoundError) as exc:
        parser.exit(2, f"错误：{exc}\n")
    except RuntimeError as exc:
        from finance_quant.data.managed import DatasetDownloadError
        from finance_quant.data.providers import TushareProviderError
        if isinstance(exc, (DatasetDownloadError, TushareProviderError)):
            parser.exit(2, f"错误：{exc}\n")
        raise


if __name__ == "__main__":
    main()
