"""Remote acquisition, offline research, and a local research web service."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from filelock import Timeout


def main() -> None:
    from finance_quant.runtime import configure_runtime
    configure_runtime()
    parser = argparse.ArgumentParser(description="量化研究工作台（Qlib 后端）")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="运行合成样本，不联网")
    demo.add_argument("--runs-dir", type=Path, default=Path("runs"))
    demo.add_argument("--data-dir", type=Path, default=Path("data/sample-qlib"))
    commands.add_parser("probe", help="验证Tushare服务能力，不回显密钥")
    for name in ['index-download','index-plan','index-status']:
        command=commands.add_parser(name,help='沪深300历史成分数据仓：下载续传、准备计划或查看状态')
        command.add_argument('--data-dir',type=Path,default=Path('data/warehouse/csi300-2020'))
        if name!='index-status':
            command.add_argument('--start',default='2020-01-01')
            command.add_argument('--end')
            command.add_argument('--warmup-sessions',type=int,default=127)
            command.add_argument('--limit',type=int,help='本次最多处理的股票数，用于小范围验收')
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
            command.add_argument("--primary-cache-from", type=Path, help="只读复用另一数据目录中同源、同参数且校验和正确的原始分片")
    real = commands.add_parser("real", help="从本地冻结快照计算因子并运行Qlib，不联网")
    real.add_argument("--config", type=Path)
    real.add_argument("--data-dir", type=Path)
    real.add_argument("--runs-dir", type=Path)
    for field in ["start", "end"]:
        real.add_argument(f"--{field}")
    for field in ["topk", "n-drop"]:
        real.add_argument(f"--{field}", type=int)
    for field in ["initial-cash", "risk-degree", "open-cost", "close-cost", "min-cost",'commission-rate','stamp-tax-rate','transfer-fee-rate','impact-cost']:
        real.add_argument(f"--{field}", type=float)
    real.add_argument('--rebalance-frequency',choices=['daily','weekly','monthly'])
    real.add_argument('--cost-mode',choices=['legacy','itemized'])
    real.add_argument('--tax-mode',choices=['historical','fixed'])
    evaluate = commands.add_parser("evaluate", help="离线评估因子IC、分组收益与相关性，保存JSON和报告")
    evaluate.add_argument("--config", type=Path, required=True)
    experiment = commands.add_parser("experiment", help="冻结候选与区间，按验证集选方案后运行独立测试区间")
    experiment.add_argument("--config", type=Path, required=True)
    experiment.add_argument("--plan", type=Path, required=True)
    serve = commands.add_parser("serve", help="启动本机研究工作台、任务队列和结果查询")
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
                         "initial_cash", "risk_degree", "open_cost", "close_cost", "min_cost",'rebalance_frequency','cost_mode','commission_rate','tax_mode','stamp_tax_rate','transfer_fee_rate','impact_cost']
            if any(getattr(args, field, None) is not None for field in overrides):
                parser.error("使用 --config 时请在配置文件内修改参数，不要混用同类命令行覆盖项")
        if args.command == "demo":
            from finance_quant.integrations.qlib_demo import run_demo
            output(run_demo(args.runs_dir, args.data_dir))
        elif args.command in {'index-download','index-plan','index-status'}:
            from finance_quant.application.index_download import run_index_download,index_download_status
            if args.command=='index-status':
                from finance_quant.application.index_download_manager import status
                output(status(args.data_dir))
            else:
                try:
                    result=run_index_download(args.data_dir,args.start,args.end,args.warmup_sessions,
                        prepare_only=args.command=='index-plan',limit=args.limit)
                finally:
                    if args.command=='index-download' and (args.data_dir/'progress.json').exists():
                        from finance_quant.application.index_download_reports import refresh_handling_report
                        try:
                            refresh_handling_report(args.data_dir)
                        except (OSError,ValueError,KeyError,Timeout):
                            print('处理报告暂未生成；下载结果已保留，可在网页重新读取报告。')
                output({k:v for k,v in result.items() if k not in {'assets','tasks','requests','reference_files'}})
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
                        "secondary_history_queries_without_cache": 2 * len(data.codes) if data.validation.mode != 'off' else 0,
                        "note": "仅预览，不联网；请求上限不含缺失记录补抓与重试。行情按年分片、按月校验；已完成分片复用，边界年份变化需重新获取。固定池并非历史成分。"})
            else:
                result = download_dataset(data, refresh=args.refresh, cache_from=args.primary_cache_from)
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
                    **{key: getattr(args, key) for key in tuple(BacktestConfig.__dataclass_fields__)[2:]
                       if getattr(args, key) is not None})
                output(run_real(args.data_dir or Path("data/tushare/20260803-20260930"), args.runs_dir or Path("runs"), **asdict(defaults)))
        elif args.command == "evaluate":
            from finance_quant.application.evaluation_workflow import evaluate_configured
            output(evaluate_configured(config))
        elif args.command == "experiment":
            from finance_quant.application.experiment_workflow import run_experiment
            from finance_quant.research.experiments import load_experiment_plan
            output(run_experiment(config, load_experiment_plan(args.plan)))
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
