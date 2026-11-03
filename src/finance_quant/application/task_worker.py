"""A single local web research task; Qlib runs outside the API process."""

from __future__ import annotations

from finance_quant.runtime import configure_runtime
configure_runtime()

import argparse
import re
import traceback
import errno
from pathlib import Path

from filelock import FileLock

from finance_quant.application.evaluation_workflow import evaluate_configured
from finance_quant.application.factor_workflow import publish_factor_run
from finance_quant.application.real_workflow import run_configured
from finance_quant.application.research_requests import project_snapshot, request_config, validate_research_data
from finance_quant.application.tasks import TaskStore
from finance_quant.application.download_diagnostics import describe_download_failure
from finance_quant.configuration import DataConfig
from finance_quant.data.managed import DatasetDownloadError, download_dataset
from finance_quant.data.market import load_market
from finance_quant.data.validation_config import CrosscheckConfig
from finance_quant.application.research_readiness import validate_download_calendar


def safe_error(error):
    from finance_quant.data.network_budget import DownloadPaused
    if isinstance(error, DatasetDownloadError):
        return "数据下载或联合校验未通过，请检查服务权限、股票代码与日期；重新运行会复用已完成缓存。"
    if isinstance(error,OSError) and (error.errno==errno.ENOSPC or getattr(error,'winerror',None) in (39,112)):
        return '存储空间不足，无法写入研究结果；请释放相关磁盘空间后重试，任务参数已保留。'
    if isinstance(error, (ValueError,DownloadPaused)):
        text = str(error)[:500]
        text = re.sub(r"(?:tsr_|sk-)[A-Za-z0-9_-]+", "[已隐藏]", text)
        text = re.sub(r"(?i)(token|password|authorization|secret)\s*[:=]\s*\S+", r"\1=[已隐藏]", text)
        return text
    if isinstance(error,MemoryError):return '研究计算内存不足，已保留任务参数，请重试。'
    return f'研究计算出现内部错误（{type(error).__name__}），已保存失败阶段与诊断信息。'


def runtime_failure_details(error,stage,identifier):
    labels={'prepare':'准备数据','factors':'计算因子','backtest':'回测','evaluation':'因子评估','download':'下载数据'}
    summary=(f'{labels.get(stage,stage)}内存不足，已保留任务参数，请重试。' if isinstance(error,MemoryError) else safe_error(error))
    # Save locations without exception arguments, local variables or credentials.
    stack=[{'file':Path(frame.filename).name,'function':frame.name,'line':frame.lineno}
           for frame in traceback.extract_tb(error.__traceback__)]
    traceback.clear_frames(error.__traceback__)
    return {'kind':'runtime','job_id':identifier,'issues':[],'stage':stage,'exception_type':type(error).__name__,
            'summary':summary,'trace':stack}


def run_task(runs_root: Path, data_root: Path, identifier: str):
    store = TaskStore(runs_root)
    with FileLock(runs_root / "tasks/execution.lock", timeout=0):
        task = store.get(identifier, private=True)
        if task["status"] != "running":
            raise ValueError("Only a claimed task can execute")
        request, execution = task["request"], task["execution"]
        completed = 0
        try:
            if execution['mode']=='index_warehouse':
                from finance_quant.application.index_download_manager import warehouse_root
                from finance_quant.application.historical_workflow import run_historical_research
                root=Path(execution['warehouse_dir']).resolve()
                from finance_quant.application.market_download import warehouse_roots
                if root not in warehouse_roots(data_root):raise ValueError('历史数据仓路径不合法')
                result=run_historical_research(request,root,runs_root,expected_sha=execution['manifest_sha256'],
                    stage=lambda phase,message,count:store.stage(identifier,phase,message,count))
                store.finish(identifier,'succeeded',result=result)
                return
            if execution["mode"] == "download":
                store.stage(identifier, "download", "下载并校验股票池数据，较长区间可能需要数分钟", 0)
                obj = request["data"]
                root = Path(execution["download_root"])
                if not root.resolve().is_relative_to((data_root / "research/downloads").resolve()):
                    raise ValueError("Invalid download destination")
                data = DataConfig(root, obj["start"], obj["end"], obj["codes"], obj["benchmark"], CrosscheckConfig(obj["validation_mode"]))
                cache_from = tuple(path for path in [data_root, *sorted(data_root.glob('research/downloads/*'))]
                                   if path.resolve().is_relative_to(data_root.resolve()) and path.resolve() != root.resolve()
                                   and (path / 'catalog.sqlite3').is_file())
                download_dataset(data, cache_from=cache_from,
                                 calendar_validator=lambda calendar: validate_download_calendar(request_config(request,data,runs_root),calendar))
                parent = load_market(root)[3]
                snapshot_dir = Path(parent["snapshot_dir"])
                completed = 1
            else:
                snapshot_dir = Path(execution["snapshot_dir"])
                if not snapshot_dir.resolve().is_relative_to(data_root.resolve()):
                    raise ValueError("Invalid snapshot location")
                parent = load_market(snapshot_dir)[3]
                if parent["id"] != execution["data_version"] or parent["files"] != execution["files"]:
                    raise ValueError("排队期间数据版本发生变化，请刷新数据列表后重新提交")
            store.stage(identifier, "prepare", "准备所选股票池并检查预热数据", completed)
            root = project_snapshot(snapshot_dir, tuple(request["data"]["codes"]), data_root)
            market, _, calendar, manifest = load_market(root)
            data = DataConfig(root, manifest["start"], manifest["end"], manifest["codes"], manifest.get("benchmark", "000300.SH"),
                              CrosscheckConfig(manifest.get("cross_validation", {}).get("mode", "off")))
            config = request_config(request, data, runs_root)
            validate_research_data(config, market, calendar)
            completed += 1
            store.stage(identifier, "factors", "计算因子原始值、评分与排名", completed)
            factors = publish_factor_run(market, manifest, runs_root, config.factors, config.neutralization)
            completed += 1
            store.stage(identifier, "backtest", "Qlib 正在模拟交易、费用和账户净值", completed)
            backtest = run_configured(config)
            completed += 1
            store.stage(identifier, "evaluation", "计算因子 IC、分组收益与诊断报告", completed)
            evaluation = evaluate_configured(config)
            if not (factors["metadata"]["id"] == backtest["factor_run_id"] == evaluation["factor_run_id"]):
                raise ValueError("Factor references differ across research outputs")
            store.finish(identifier, "succeeded", result={"data_version": manifest["id"],
                         "factor_run_id": factors["metadata"]["id"], "backtest_run_id": backtest["run_id"],
                         "evaluation_run_id": evaluation["run_id"], "backtest_reused": backtest["reused"],
                         "evaluation_reused": evaluation["reused"]})
        except Exception as error:
            details = None
            if execution['mode'] == 'download':
                try:
                    details = describe_download_failure(Path(execution['download_root']), since=task['started_at'])
                except Exception:
                    # A broken diagnostic catalog must not hide the original failure.
                    pass
            if details is None:details=runtime_failure_details(error,store.get(identifier)['stage'],identifier)
            store.finish(identifier, "failed", error=details['summary'], error_details=details)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()
    run_task(args.runs_root.resolve(), args.data_root.resolve(), args.task_id)


if __name__ == "__main__":
    main()
