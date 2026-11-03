"""Versioned official SW industry intervals; never backfill today's labels."""
from __future__ import annotations
import hashlib, json, os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import tempfile
import pandas as pd
from filelock import FileLock
from finance_quant.data.managed import atomic_json

VERSION = 'sw-l1-history-v1'
TRANSITION = pd.Timestamp('2021-12-13')
LEGACY_START = pd.Timestamp('2015-01-01')
POLICY = ('申万一级；SW2014研究下界2015-01-01、截至2021-12-10，SW2021最早2021-12-13（保守版本切换约定，非公告认证）；'
          '记录纳入、剔除日期后的下一交易日起更新；不回填当前行业，冲突及缺失单列；'
          '日期为供应商事后历史记录，实际公告发布时间未认证')

def industry_root():
    return Path(os.environ.get('FINANCE_QUANT_INDUSTRY_ROOT') or
                Path(__file__).resolve().parents[3]/'data/industries/official-tushare')

def normalize_intervals(parts):
    rows=[]
    for taxonomy, code, name, raw in parts:
        stock='con_code' if taxonomy=='SW2014' else 'ts_code'
        if raw.empty: continue
        if not {stock,'in_date','out_date'} <= set(raw): raise ValueError('历史行业缺少日期或代码字段')
        for r in raw.to_dict('records'):
            symbol=str(r[stock]); start=pd.to_datetime(r['in_date'],format='%Y%m%d',errors='coerce')
            end=pd.to_datetime(r['out_date'],format='%Y%m%d',errors='coerce')
            if pd.notna(r['out_date']) and str(r['out_date']).strip() and pd.isna(end):
                raise ValueError('历史行业剔除日期无效')
            if len(symbol)!=9 or symbol[-2:] not in {'SH','SZ','BJ'} or pd.isna(start):
                raise ValueError('历史行业代码或纳入日期无效')
            if pd.notna(end) and end<start: raise ValueError('历史行业剔除早于纳入')
            rows.append(dict(instrument=symbol[-2:]+symbol[:6],industry_code=code,industry_name=name,
                classification=taxonomy,in_date=start,out_date=end))
    if not rows: raise ValueError('历史行业数据为空')
    return pd.DataFrame(rows).drop_duplicates().sort_values(['instrument','classification','in_date','industry_code']).reset_index(drop=True)

def _atomic_parquet(frame,path):
    descriptor,name=tempfile.mkstemp(dir=path.parent,suffix='.parquet');os.close(descriptor)
    temporary=Path(name)
    try:
        frame.to_parquet(temporary,index=False)
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)

def publish_industries(frame, *, root=None, evidence=None, data_asof=None):
    root=Path(root or industry_root());root.mkdir(parents=True,exist_ok=True)
    payload=frame.to_csv(index=False,date_format='%Y-%m-%d').encode()
    data_asof=data_asof or datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    digest=hashlib.sha256(payload+VERSION.encode()+POLICY.encode()+data_asof.encode()).hexdigest()
    directory=root/digest;directory.mkdir(exist_ok=True)
    path=directory/'intervals.parquet'
    if not path.exists():_atomic_parquet(frame,path)
    manifest={'id':digest,'schema_version':VERSION,'file':'intervals.parquet',
        'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'rows':len(frame),
        'instruments':int(frame.instrument.nunique()),'source':'official_tushare',
        'created_at':datetime.now(timezone.utc).isoformat(),'policy':POLICY,'data_asof':data_asof,
        'point_in_time_certified':False,'evidence':evidence or {}}
    if not (directory/'manifest.json').exists():atomic_json(directory/'manifest.json',manifest)
    atomic_json(root/'current.json',{'id':digest})
    return load_industries(root=root)

def load_industries(version=None, *, root=None):
    root=Path(root or industry_root())
    if version is None: version=json.loads((root/'current.json').read_text(encoding='utf-8'))['id']
    if not isinstance(version,str) or len(version)!=64 or any(c not in '0123456789abcdef' for c in version):
        raise ValueError('行业版本无效')
    directory=root/version;manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    path=directory/'intervals.parquet'
    if manifest.get('id')!=version or manifest.get('schema_version')!=VERSION or hashlib.sha256(path.read_bytes()).hexdigest()!=manifest['sha256']:
        raise ValueError('历史行业缓存校验失败')
    frame=pd.read_parquet(path)
    payload=frame.to_csv(index=False,date_format='%Y-%m-%d').encode()+VERSION.encode()
    if 'data_asof' in manifest:payload+=manifest['policy'].encode()+manifest['data_asof'].encode()
    if hashlib.sha256(payload).hexdigest()!=version or manifest.get('point_in_time_certified') is not False:
        raise ValueError('历史行业版本或时点声明校验失败')
    return IndustryHistory(frame,manifest)

def optional_industries(version=None):
    try:return load_industries(version)
    except FileNotFoundError:
        if version:raise ValueError('所选历史行业版本不存在，请重新下载历史行业数据') from None
        return None

class IndustryHistory:
    def __init__(self,frame,manifest):
        self.frame=frame.copy();self.manifest=manifest
        self._cache={}

    @property
    def data_asof(self):
        return self.manifest.get('data_asof') or (self.manifest.get('created_at') or '')[:10] or None

    def validate_range(self,end):
        if self.data_asof and pd.Timestamp(end)>pd.Timestamp(self.data_asof):
            raise ValueError(f'历史行业数据仅核验至{self.data_asof}，请刷新历史行业缓存后重试；不会继续套用旧标签')

    def at(self,date,instruments,calendar):
        date=pd.Timestamp(date)
        if date not in pd.DatetimeIndex(calendar):raise ValueError('行业匹配日期不在交易日历中')
        if date<LEGACY_START or (self.data_asof and date>pd.Timestamp(self.data_asof)):
            return {symbol:{'industry_code':None,'industry_name':None,'classification':None,
                'industry_status':'missing'} for symbol in instruments}
        # Signals use only records dated strictly before this session. Date-only
        # changes are conservatively applied on the following exchange session.
        taxonomy='SW2014' if date<TRANSITION else 'SW2021'
        key=(date,taxonomy)
        if key not in self._cache:
            f=self.frame
            active=f.loc[f.classification.eq(taxonomy)&f.in_date.lt(date)&(f.out_date.isna()|f.out_date.ge(date))]
            unique=active[['instrument','industry_code','industry_name']].drop_duplicates()
            conflict=unique.instrument.duplicated(keep=False)
            mapped={row.instrument:{'industry_code':row.industry_code,'industry_name':row.industry_name,
                'classification':taxonomy,'industry_status':'known'} for row in unique.loc[~conflict].itertuples()}
            mapped.update({instrument:{'industry_code':None,'industry_name':None,
                'classification':taxonomy,'industry_status':'conflict'} for instrument in unique.loc[conflict,'instrument'].unique()})
            self._cache[key]=mapped
            if len(self._cache)>8:self._cache.pop(next(iter(self._cache)))
        return {symbol:self._cache[key].get(symbol,{'industry_code':None,'industry_name':None,
                'classification':taxonomy,'industry_status':'missing'}) for symbol in instruments}

def download_industries(*,root=None,provider=None,progress=None,refresh=False):
    """Serial, restartable bounded requests; full historical Y/N sets, not survivors."""
    from finance_quant.data.providers.tushare import TushareProvider,TushareSettings
    root=Path(root or industry_root())
    raw_dir=root/'raw'
    if refresh:raw_dir=raw_dir/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    raw_dir.mkdir(parents=True,exist_ok=True)
    provider=provider or TushareProvider(TushareSettings(TushareSettings.from_env().token))
    parts=[];evidence={};progress=progress or (lambda x:None)
    with FileLock(root/'download.lock',timeout=0):
        for taxonomy in ['SW2014','SW2021']:
            catalog=raw_dir/f'{taxonomy}.parquet'
            if not catalog.exists():_atomic_parquet(provider.query('index_classify',level='L1',src=taxonomy),catalog)
            sectors=pd.read_parquet(catalog)
            if sectors.empty:raise ValueError('历史行业分类目录为空')
            for sector in sectors.to_dict('records'):
                api='index_member' if taxonomy=='SW2014' else 'index_member_all'
                code=sector['index_code']
                for state in ['Y','N']:
                    path=raw_dir/f'{taxonomy}-{code}-{state}.parquet'
                    if not path.exists():
                        params={('index_code' if taxonomy=='SW2014' else 'l1_code'):code,'is_new':state}
                        frame=provider.query(api,**params)
                        if len(frame)>=2000:raise ValueError('历史行业触及单次上限，拒绝发布截断数据')
                        _atomic_parquet(frame,path)
                    frame=pd.read_parquet(path)
                    if len(frame)>=2000:raise ValueError('历史行业触及单次上限，拒绝发布截断数据')
                    code_field='index_code' if taxonomy=='SW2014' else 'l1_code'
                    if not frame.empty and (code_field not in frame or not frame[code_field].eq(code).all()):
                        raise ValueError('历史行业响应分类代码不匹配')
                    parts.append((taxonomy,code,sector['industry_name'],frame));evidence[path.name]={'rows':len(frame),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
                    progress({'taxonomy':taxonomy,'code':code,'state':state,'rows':len(frame)})
        files=list(raw_dir.glob('*.parquet'))
        oldest=min(p.stat().st_mtime for p in files)
        data_asof=datetime.fromtimestamp(oldest,ZoneInfo('Asia/Shanghai')).date().isoformat()
        return publish_industries(normalize_intervals(parts),root=root,evidence=evidence,data_asof=data_asof)
