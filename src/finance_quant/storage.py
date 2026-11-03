"""Immutable, JSON-safe local research artifacts; independent of Qlib internals."""

from __future__ import annotations

import json
import os
import re
import tempfile
import copy
from functools import lru_cache
from pathlib import Path
from typing import Any

ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")


@lru_cache(maxsize=256)
def _listing_record(filename,category,modified,size,created):
    """Cache small listing records, never retain whole legacy histories."""
    document=json.loads(Path(filename).read_text(encoding='utf-8'))
    if not isinstance(document,dict) or not isinstance(document.get('metadata'),dict):raise ValueError('Invalid artifact metadata')
    metadata=dict(document['metadata'])
    if category=='backtests':
        from finance_quant.research.performance import enrich_backtest
        summary=enrich_backtest(document)['summary']
        summary={**summary,'id':Path(filename).parent.name}
        for key in ['name','status','strategy','start_date','end_date','created_at','is_synthetic','metrics']:summary[key]
        if any(not isinstance(summary[k],str) for k in ['name','status','strategy','start_date','end_date','created_at']):raise ValueError('Invalid backtest fields')
        if not isinstance(summary['is_synthetic'],bool) or not isinstance(summary['metrics'],(dict,type(None))):raise ValueError('Invalid backtest fields')
        for key in ['name','status','strategy','start_date','end_date','created_at','is_synthetic','metrics']:summary[key]
        return {'summary':summary,'metadata':metadata}
    metadata['id']=Path(filename).parent.name
    if category=='factors':
        history=document.get('history',[])
        if not isinstance(history,list):raise ValueError('Invalid factor history')
        if not metadata.get('available_dates'):metadata['available_dates']=sorted({r['date'] for r in history})
        if 'universe_members' not in metadata:
            members={}
            for row in history:members.setdefault(row['instrument'],{k:row[k] for k in ['instrument','name']})
            metadata['universe_members']=list(members.values())
        for key in ['membership_by_execution_date','coverage_by_date','availability_by_date','lifecycle_exclusions']:metadata.pop(key,None)
        for key in ['scheme','as_of','weights','is_synthetic']:metadata[key]
        if not isinstance(metadata['weights'],dict) or not isinstance(metadata['universe_members'],list):raise ValueError('Invalid factor metadata')
        if any(not isinstance(row,dict) or not all(isinstance(row.get(k),str) for k in ['instrument','name']) for row in metadata['universe_members']):
            raise ValueError('Invalid factor instruments')
        if not isinstance(metadata['weights'],dict) or not isinstance(metadata['universe_members'],list):raise ValueError('Invalid factor metadata')
        if any(not isinstance(row,dict) or not all(isinstance(row.get(k),str) for k in ['instrument','name']) for row in metadata['universe_members']):
            raise ValueError('Invalid factor instruments')
        if any(not isinstance(metadata[k],str) for k in ['scheme','as_of']) or not isinstance(metadata['available_dates'],list) or any(not isinstance(d,str) for d in metadata['available_dates']):
            raise ValueError('Invalid factor dates')
    else:
        for key in ['created_at','name','is_synthetic']:metadata[key]
    if not isinstance(metadata.get('created_at',''),str) or not isinstance(metadata.get('is_synthetic'),bool):raise ValueError('Invalid listing fields')
    return {'metadata':metadata}


def runs_root() -> Path:
    return Path(os.environ.get("FINANCE_QUANT_RUNS", "runs")).resolve()


class ResultStore:
    def __init__(self, root: Path | None = None):
        self.root = (root or runs_root()).resolve()

    def path(self, category: str, run_id: str) -> Path:
        if category not in {"backtests", "factors", "evaluations", "experiments"} or not ID_PATTERN.fullmatch(run_id):
            raise ValueError("Invalid artifact identifier")
        result = (self.root / category / run_id / "result.json").resolve()
        if not result.is_relative_to(self.root):
            raise ValueError("Artifact outside result directory")
        return result

    def get(self, category: str, run_id: str, *, hydrate=True) -> dict[str, Any]:
        result=json.loads(self.path(category, run_id).read_text(encoding="utf-8"))
        if hydrate and result.get('tables'):
            from finance_quant.storage_tables import hydrate_tables
            hydrate_tables(self,category,run_id,result)
        return result

    def list(self, category: str) -> list[dict[str, Any]]:
        directory = self.root / category
        if not directory.exists():
            return []
        results = []
        for child in sorted(directory.iterdir()):
            if child.is_dir() and ID_PATTERN.fullmatch(child.name):
                try:
                    results.append(self.get(category, child.name,hydrate=False))
                except FileNotFoundError:
                    # A task may have created its directory but not published yet.
                    continue
        return results

    def list_metadata(self,category):
        self.path(category,'listing-validation')
        directory=self.root/category;items=[];unavailable=[]
        if not directory.exists():return items,unavailable
        for child in sorted(directory.iterdir()):
            if not child.is_dir() or not ID_PATTERN.fullmatch(child.name):continue
            try:
                path=self.path(category,child.name);stat=path.stat()
                record=_listing_record(str(path),category,stat.st_mtime_ns,stat.st_size,stat.st_ctime_ns)
                items.append(copy.deepcopy(record))
            except FileNotFoundError:continue # Not yet atomically published.
            except (OSError,ValueError,KeyError,TypeError,AttributeError):
                unavailable.append({'id':child.name,'category':category,'reason':'结果文件损坏、格式异常或暂不可读；已隔离显示'})
        return items,unavailable

    def publish(self, category: str, run_id: str, document: dict[str, Any]) -> Path:
        """Publish a complete artifact atomically; never silently overwrite a run."""
        destination = self.path(category, run_id)
        destination.parent.mkdir(parents=True,exist_ok=True)
        descriptor,staging=tempfile.mkstemp(dir=destination.parent,suffix='.tmp')
        try:
            with os.fdopen(descriptor,'w',encoding='utf-8') as file:
                json.dump(document,file,ensure_ascii=False,allow_nan=False,indent=2)
                file.flush();os.fsync(file.fileno())
            try:os.link(staging,destination)
            except FileExistsError:
                if json.loads(destination.read_text(encoding='utf-8'))!=document:
                    raise FileExistsError(f'Artifact {run_id} already exists with different content') from None
        finally:Path(staging).unlink(missing_ok=True)
        return destination

    def publish_report(self, run_id: str, markdown: str, *, category: str = "evaluations") -> Path:
        return self._publish_text(self.path(category, run_id).with_name("report.md"), markdown)

    def publish_experiment_record(self, run_id: str, name: str, document: dict[str, Any]) -> Path:
        if name not in {"plan", "selection"}:
            raise ValueError("Unsupported experiment record")
        destination = self.path("experiments", run_id).with_name(name + ".json")
        return self._publish_text(destination, json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2), json_document=True)

    def _publish_text(self, destination: Path, encoded: str, *, json_document: bool = False) -> Path:
        def matches() -> bool:
            existing = destination.read_text(encoding="utf-8")
            return json.loads(existing) == json.loads(encoded) if json_document else existing == encoded

        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if matches():
                return destination
            raise FileExistsError(f"Artifact {destination.parent.name} already exists with different content")
        descriptor, staging = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            # A hard link makes creation atomic and refuses replacing an existing run.
            try:
                os.link(staging, destination)
            except FileExistsError:
                if not matches():
                    raise
        finally:
            Path(staging).unlink(missing_ok=True)
        return destination
