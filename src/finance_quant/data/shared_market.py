"""Immutable verified quote segments, independent of universe membership."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
from filelock import FileLock

from finance_quant.data.managed import atomic_json
from finance_quant.data.temporal_audit import restore_valuation_dates

FIELDS = {'instrument','date','open','high','low','close','pre_close','volume','amount',
          'change','adj_factor','pe_ttm','pb','total_mv','up_limit','down_limit','is_suspended'}
POOL_FIELDS = {'index_member','selection_member','eligible_history','has_required_pre_history','history_sessions_before'}
VERSION = 'shared-market-v1'


def validation_counts(frame):
    counts={level:0 for level in ['single_source_passed','dual_source_verified','secondary_fallback_passed']}
    for _,part in frame.groupby('instrument',sort=False):
        levels=set(part.validation_level)
        level='single_source_passed' if 'single_source_passed' in levels else 'secondary_fallback_passed' if 'secondary_fallback_passed' in levels else 'dual_source_verified'
        counts[level]+=1
    return counts


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def catalog(root):
    path=Path(root)/'catalog.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'schema_version':VERSION,'segments':{},'imports':{}}


def register_warehouse(root, warehouse):
    """Register verified original files by reference; never edit/copy the warehouse."""
    root,warehouse=Path(root),Path(warehouse).resolve()
    root.mkdir(parents=True,exist_ok=True)
    manifest_path=warehouse/'manifest.json'
    if not manifest_path.exists():return 0
    checksum=digest(manifest_path)
    with FileLock(root/'catalog.lock'):
        document=catalog(root)
        if document['imports'].get(str(warehouse))==checksum:return 0
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest.get('schema_version')!='historical-index-warehouse-v1':raise ValueError('只登记已发布历史仓库')
        additions=[]
        for code,asset in manifest['assets'].items():
            path=(warehouse/asset['file']).resolve()
            if not path.is_relative_to(warehouse) or digest(path)!=asset['sha256']:raise ValueError(f'{code}原仓库校验失败')
            columns=set(pq.read_schema(path).names)
            if not FIELDS.issubset(columns):raise ValueError(f'{code}共享字段不完整')
            additions.append((code,{**asset,'path':str(path),'columns':sorted(columns-POOL_FIELDS),
                                   'origin_manifest':checksum,'origin':'legacy_warehouse'}))
        for code,asset in additions:
            rows=document['segments'].setdefault(code,[])
            if not any(r['sha256']==asset['sha256'] and r['path']==asset['path'] for r in rows):rows.append(asset)
        document['imports'][str(warehouse)]=checksum
        atomic_json(root/'catalog.json',document)
    return len(additions)


def register_segment(root,record,path):
    path=Path(path).resolve();root=Path(root).resolve()
    if not path.is_relative_to(root) or digest(path)!=record['sha256']:raise ValueError('新行情分区校验失败')
    columns=set(pq.read_schema(path).names)
    if not FIELDS.issubset(columns):raise ValueError('新行情分区缺少必要字段')
    candidate=pd.read_parquet(path,columns=['date','open','high','low','close','adj_factor'])
    days=pd.DatetimeIndex(candidate.date)
    overlap=covered_dates(catalog(root),record['code'],days,record['provider_id'])
    if len(overlap):
        existing,_=read_asset(root,record['code'],overlap,record['provider_id'])
        existing=existing.set_index('date');proposed=candidate.set_index('date').loc[overlap]
        for field in ['open','high','low','close','adj_factor']:
            if ((proposed[field]-existing.loc[overlap,field]).abs()>(existing.loc[overlap,field].abs()*1e-7+1e-8)).any():
                raise ValueError(f"{record['code']}增量边界{field}冲突，新分区已保留，未加入共享覆盖")
    with FileLock(root/'catalog.lock'):
        document=catalog(root);rows=document['segments'].setdefault(record['code'],[])
        if not any(r['sha256']==record['sha256'] and r['path']==str(path) for r in rows):
            rows.append({**record,'path':str(path),'columns':sorted(columns-POOL_FIELDS),'origin':'shared_download'})
        atomic_json(root/'catalog.json',document)


def covered_dates(document,code,sessions,provider_id):
    covered=pd.DatetimeIndex([])
    for asset in document['segments'].get(code,[]):
        if asset['provider_id']!=provider_id or not FIELDS.issubset(asset['columns']):continue
        covered=covered.union(sessions[(sessions>=pd.Timestamp(asset['start']))&(sessions<=pd.Timestamp(asset['end']))])
    return covered


def gap_ranges(expected,covered):
    missing=~expected.isin(covered);ranges=[];left=None
    for i,flag in enumerate(missing):
        if flag and left is None:left=i
        if left is not None and (not flag or i==len(missing)-1):
            right=i if flag else i-1
            ranges.append((expected[left],expected[right]));left=None
    return ranges


def read_asset(root,code,expected,provider_id):
    """First published values win; incompatible overlap is an error, never overwritten."""
    parts=[];sources=[]
    for asset in catalog(root)['segments'].get(code,[]):
        if asset['provider_id']!=provider_id or not FIELDS.issubset(asset['columns']):continue
        if pd.Timestamp(asset['end'])<expected.min() or pd.Timestamp(asset['start'])>expected.max():continue
        path=Path(asset['path'])
        if digest(path)!=asset['sha256']:raise ValueError(f'{code}共享行情校验失败，旧版本未修改')
        columns=[c for c in pq.read_schema(path).names if c not in POOL_FIELDS]
        frame=pd.read_parquet(path,columns=columns)
        frame=restore_valuation_dates(frame)
        frame=frame.loc[frame.date.isin(expected)]
        if frame.empty:continue
        if parts:
            previous=pd.concat(parts,ignore_index=True).drop_duplicates('date').set_index('date')
            overlap=frame.loc[frame.date.isin(previous.index)].set_index('date')
            for field in ['open','high','low','close','adj_factor']:
                a=overlap[field];b=previous.loc[a.index,field]
                if ((a-b).abs()>(b.abs()*1e-7+1e-8)).any():
                    raise ValueError(f'{code}增量边界{field}与已核验版本冲突，保留旧版本并等待复核')
        parts.append(frame);sources.append({k:asset[k] for k in ['path','sha256','validation_level','start','end']})
    if not parts:raise ValueError(f'{code}没有可用共享行情')
    frame=pd.concat(parts,ignore_index=True).drop_duplicates('date',keep='first').sort_values('date').reset_index(drop=True)
    if not pd.DatetimeIndex(frame.date).equals(expected):raise ValueError(f'{code}共享行情仍有缺口')
    return frame,sources


def validate_snapshot(market,manifest):
    """Shared projections preserve per-row source levels, never invent dual verification."""
    from finance_quant.data.suspensions import invalid_halt_rows
    if manifest.get('source')!='official_tushare_shared' or manifest.get('coverage',{}).get('status')!='complete':
        raise ValueError('共享研究快照未通过覆盖检查')
    if not {'price_source','adjustment_source','valuation_source','limit_source','validation_level'}.issubset(market):
        raise ValueError('共享研究快照缺少逐行来源')
    if not market.validation_level.isin(['single_source_passed','dual_source_verified','secondary_fallback_passed']).all():
        raise ValueError('共享研究快照存在未核验行')
    if invalid_halt_rows(market).any():raise ValueError('共享研究快照停牌估值无效')
    for field in ['price_source','adjustment_source']:
        if not market[field].isin(['tushare','baostock']).all():raise ValueError('共享研究快照来源无效')
        fallback=market.validation_level.eq('secondary_fallback_passed')
        if not market.loc[fallback,field].eq('baostock').all() or not market.loc[~fallback,field].eq('tushare').all():
            raise ValueError('共享研究快照核验等级与来源不一致')
    if not market.valuation_source.eq('tushare').all() or not market.limit_source.eq('tushare').all():raise ValueError('估值和限价来源无效')
