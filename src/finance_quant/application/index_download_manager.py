"""One resumable background warehouse worker; status and cooperative pause."""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path
from filelock import FileLock,Timeout
from finance_quant.application.index_download import index_download_status
from finance_quant.data.managed import atomic_json

def warehouse_root():return Path(__file__).resolve().parents[3]/'data/warehouse/csi300-2020'

def _active(root):
    if not root.exists():return False
    try:
        with FileLock(root/'warehouse.lock',timeout=0):pass
    except Timeout:return True
    runner=root/'runner.json'
    if runner.exists():
        import psutil
        try:
            record=json.loads(runner.read_text());process=psutil.Process(record['pid'])
            return process.is_running() and abs(process.create_time()-record['process_created'])<.1
        except (OSError,ValueError,KeyError,psutil.Error):pass
    return False

def status(root=None):
    root=Path(root) if root else warehouse_root()
    result=index_download_status(root)
    result['worker_running']=_active(root)
    result['pause_requested']=(root/'PAUSE').exists()
    result['verified']=result.get('completed',0)
    result['failed']=len(result.get('failures',{}))
    result['limits']={'baostock_daily_requests':10000,'baostock_per_minute':60,
        'tushare_per_minute':120,'tushare_daily_per_api':90000}
    result['failures']=[{'code':code,**value} for code,value in list(result.get('failures',{}).items())[:30]]
    result.pop('assets',None)
    return result

def start(root=None):
    root=Path(root) if root else warehouse_root();root.mkdir(parents=True,exist_ok=True)
    with FileLock(root/'launch.lock',timeout=0):
        if _active(root):return status(root)
        (root/'PAUSE').unlink(missing_ok=True)
        with (root/'worker.log').open('a',encoding='utf-8') as output:
            process=subprocess.Popen([sys.executable,'-u','-X','utf8','-m','finance_quant.cli','index-download',
                '--data-dir',str(root)],cwd=Path(__file__).resolve().parents[3],stdout=output,stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        import psutil
        atomic_json(root/'runner.json',{'pid':process.pid,'process_created':psutil.Process(process.pid).create_time()})
    return status(root)

def pause(root=None):
    root=Path(root) if root else warehouse_root();root.mkdir(parents=True,exist_ok=True)
    (root/'PAUSE').write_text('Pause before next stock; keep completed requests.\n',encoding='utf-8')
    return status(root)
