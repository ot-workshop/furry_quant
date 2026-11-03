import {
  DRAFT_KEY,
  STAGE_ORDER,
  STAGE_LABELS,
  STATUS_LABELS,
  STATUS_CLASSES,
  VALIDATION_LABELS,
  VALIDATION_OPTIONS,
  isRecord,
  unique,
  dateText,
  toNumber,
  validDate,
  parseCodes,
  clampTopk,
  buildDefaultForm,
  mergeDraft,
  readDraft,
  buildRequest,
  formFromRequest,
  makeIdempotencyKey,
  earliestBacktestDate,
  warmupInfo,
  historicalOption,
  matchingSnapshot,
  type FactorFormState,
  type FormState,
} from "./researchForm";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  describeResearchError,
  researchApi,
  type FactorName,
  type ResearchTask,
  type ResearchOptions,
  type SourceMode,
  type ValidationMode,
  type DatePlan,
  type TradingCalendar,
  type ResearchRequest,
} from "../api/research";
import { EmptyPanel, ErrorPanel, LoadingPanel } from "../components/StatePanels";
import { useApiResource } from "../hooks/useApiResource";
import { factorLabel, formatDateTime } from "../utils/format";
import "./ResearchPage.css";

export type ResultKind = "factors" | "backtests" | "evaluations";

export interface ResearchPageProps {
  onOpenResult: (kind: ResultKind, id: string, date?: string) => void;
  onResultsChanged: () => void;
}

interface TaskCardProps {
  task: ResearchTask;
  canReuse: boolean;
  cancelling: boolean;
  onCancel: (id: string) => void;
  onOpenResult: (kind: ResultKind, id: string, date?: string) => void;
  onReuse: (task: ResearchTask) => void;
  onDownload: (task: ResearchTask) => void;
}

function StageProgress({ task }: { task: ResearchTask }) {
  const stages = STAGE_ORDER.filter(stage => !["queued", "finished"].includes(stage)
    && (stage !== "download" || task.request.data.mode === "download"));
  const progress = task.progress ?? {
    completed: 0,
    total: stages.length,
  };
  const total = progress.total || stages.length;
  const completed = Math.max(0, Math.min(progress.completed, total));
  const terminalSuccess = task.status === "succeeded";

  return (
    <div className="research-stage">
      <div className="research-stage-meta">
        <span>阶段：{task.stage === "finished" && task.status !== "succeeded" ? "已结束" : STAGE_LABELS[task.stage] ?? task.stage}</span>
        <span>
          已完成 {completed} / {total} 个阶段
        </span>
      </div>
      <div
        className="research-stage-track"
        aria-label={`任务阶段，已完成 ${completed} / ${total}`}
      >
        {stages.map((stage, index) => {
          const done = terminalSuccess || index < completed;
          const current =
            !terminalSuccess &&
            task.status !== "cancelled" &&
            stage === task.stage;
          return (
            <span
              key={stage}
              className={`research-stage-step${done ? " is-done" : ""}${
                current ? " is-current" : ""
              }`}
            >
              {STAGE_LABELS[stage]}
            </span>
          );
        })}
      </div>
    </div>
  );
}

function TaskCard({
  task,
  canReuse,
  cancelling,
  onCancel,
  onOpenResult,
  onReuse,
  onDownload,
}: TaskCardProps) {
  const result = task.result;

  return (
    <article className="research-task">
      <div className="research-task-head">
        <div>
          <div className="research-task-title">
            {task.name || "未命名研究任务"}
          </div>
          <div className="research-task-sub">
            {task.id} · 创建于 {formatDateTime(task.created_at)}
          </div>
        </div>
        <div className="research-task-actions">
          <span
            className={`status-pill ${STATUS_CLASSES[task.status] ?? "status-unknown"}`}
          >
            {STATUS_LABELS[task.status] ?? task.status}
          </span>
          {task.status === "queued" ? (
            <button
              type="button"
              className="button button-compact"
              disabled={cancelling}
              onClick={() => onCancel(task.id)}
            >
              {cancelling ? "取消中…" : "取消排队"}
            </button>
          ) : null}
        </div>
      </div>

      <StageProgress task={task} />

      {task.message ? (
        <p className="muted-text research-task-message">{task.message}</p>
      ) : null}

      {task.status === "interrupted" ? (
        <p className="warning-callout">
          服务在任务运行期间重启，任务已中断。请使用“再用此配置”重新运行；中断不会按 0 结果处理。
        </p>
      ) : null}

      {task.status === "failed" ? (
        <div className="research-task-error" role="alert">
          任务失败：{task.error || "后端未提供错误详情（不会按 0 结果处理）。"}
        </div>
      ) : null}
      {task.error_details?.issues.length ? <details className="research-task-parameters">
        <summary>查看失败明细</summary>
        <p className="hint-text">下载任务：{task.error_details.job_id}</p>
        {task.error_details.issues.map((issue, index) => <p className="hint-text" key={index}>{issue.code} · {issue.description ?? `${issue.api} · ${issue.category}`}{issue.dates.length ? ` · ${issue.dates.join('、')}` : ''}</p>)}
      </details> : null}
      {task.error_details?.kind==='runtime' ? <details className="research-task-parameters">
        <summary>查看失败明细</summary>
        <p>失败阶段：{STAGE_LABELS[task.error_details.stage as keyof typeof STAGE_LABELS] ?? task.error_details.stage ?? '研究计算'}；错误类型：{task.error_details.exception_type}。</p>
        {task.error_details.trace?.map((frame,index)=><p className="hint-text" key={index}>{frame.file}:{frame.line} · {frame.function}</p>)}
      </details> : null}

      {task.status === "succeeded" && result ? (
        <div className="research-result">
          <div className="research-result-meta">
            <span>数据版本</span>
            <code>{result.data_version || "—"}</code>
          </div>
          <div className="card-actions">
            <button
              type="button"
              className="button button-compact"
              disabled={!result.factor_run_id}
              onClick={() => onOpenResult("factors", result.factor_run_id, result.factor_as_of ?? task.request.backtest.end)}
            >
              查看评分
            </button>
            <button
              type="button"
              className="button button-compact"
              disabled={!result.backtest_run_id}
              onClick={() => onOpenResult("backtests", result.backtest_run_id)}
            >
              查看回测
            </button>
            <button
              type="button"
              className="button button-compact"
              disabled={!result.evaluation_run_id}
              onClick={() =>
                onOpenResult("evaluations", result.evaluation_run_id)
              }
            >
              查看因子评估
            </button>
          </div>
        </div>
      ) : null}

      {task.status==='succeeded' && result && task.request.data.mode==='index_warehouse' ? <p>
        <a href={`/api/v1/factor-runs/${encodeURIComponent(result.factor_run_id)}/tables/history`}>下载完整评分明细（Parquet）</a>
        <span className="hint-text"> · 网页按选中日期读取；成分调出与退出受阻可在回测页查看。</span>
      </p> : null}

      {task.status === "succeeded" && !result ? (
        <div className="research-task-error" role="alert">
          任务已完成但未返回结果 ID，请刷新任务列表或重新运行；不会按 0
          结果处理。
        </div>
      ) : null}

      <details className="research-task-parameters">
        <summary>运行参数</summary>
        <p className="hint-text">评价基准：{task.request.backtest.benchmark ?? '沿用数据集原基准'}。</p>
        <p className="hint-text">股票池：{task.request.data.mode==='index_warehouse' ? '沪深300历史成分（每日动态名单，含科创板）' : task.request.data.codes.join("、")}</p>
        <p className="hint-text">数据：{task.request.data.mode === 'download' ? `${task.request.data.start} 至 ${task.request.data.end}（包含预热区间）` : task.request.data.mode==='index_warehouse' ? task.request.data.warehouse_id : `已有快照 ${task.request.data.snapshot_id}`}</p>
        <p className="hint-text">回测：{task.request.backtest.start} 至 {task.request.backtest.end}；初始资金 {task.request.backtest.initial_cash.toLocaleString()} 元；目标持有 {task.request.backtest.topk} 只。</p>
        <p className="hint-text">调仓：{({daily:'每日',weekly:'每周首个交易日',monthly:'每月首个交易日'})[task.request.backtest.rebalance_frequency ?? 'daily']}；费用：{task.request.backtest.cost_mode==='itemized' ? `分项，佣金${(100*(task.request.backtest.commission_rate ?? .0003)).toFixed(4)}%，${task.request.backtest.tax_mode==='fixed' ? '自定义税费' : '历史税费'}` : '旧版综合费率'}；双边冲击成本 {100*(task.request.backtest.impact_cost ?? 0)}%。</p>
        <p className="hint-text">行业中性化：{task.request.neutralization?.mode==='industry'?'开启（申万一级）':'关闭'}；历史行业版本：{task.request.neutralization?.industry_version?.slice(0,12) ?? '未指定'}</p>
        <p className="hint-text">因子：{task.request.factors.map(f => `${factorLabel(f.name)}${f.id ? ` [${f.id}]` : ''}${f.window == null ? "" : `（窗口${f.window}日）`}${f.skip == null ? '' : ` 剔除${f.skip}日`}${f.baseline_window == null ? '' : ` 基准${f.baseline_window}日`} ${(f.weight * 100).toFixed(2)}%`).join("；")}</p>
      </details>
      <div className="research-task-foot">
        <button
          type="button"
          className="text-button"
          disabled={!canReuse}
          onClick={() => onReuse(task)}
        >
          再用此配置
        </button>
        <button
          type="button"
          className="text-button"
          onClick={() => onDownload(task)}
        >
          下载 JSON 配置
        </button>
      </div>
    </article>
  );
}

export function ResearchPage({
  onOpenResult,
  onResultsChanged,
}: ResearchPageProps) {
  const optionsState = useApiResource(
    (signal) => researchApi.getOptions(signal),
    [],
  );
  const [previousOptions, setPreviousOptions] = useState<ResearchOptions | null>(null);
  const [extraCalendars,setExtraCalendars] = useState<TradingCalendar[]>([]);
  const serverOptions = optionsState.data ?? previousOptions;
  const options = useMemo(()=>serverOptions ? {...serverOptions,calendars:[...(serverOptions.calendars ?? []),...extraCalendars]} : null,[serverOptions,extraCalendars]);
  useEffect(() => { if (optionsState.data) setPreviousOptions(optionsState.data); }, [optionsState.data]);

  const [rawDraft] = useState<unknown>(() => readDraft());
  const [form, setForm] = useState<FormState | null>(null);
  const initializedRef = useRef(false);
  const [errors, setErrors] = useState<string[]>([]);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [planning,setPlanning] = useState(false);
  const [planError,setPlanError] = useState<string | null>(null);
  const [datePlan,setDatePlan] = useState<{key:string;value:DatePlan} | null>(null);
  // Confirmation belongs to the exact draft, including pool and trading costs.
  const dateKey = form ? JSON.stringify(form) : '';
  const dateKeyRef=useRef(dateKey);dateKeyRef.current=dateKey;
  const currentPlan=datePlan?.key===dateKey ? datePlan.value : null;
  const automaticDates = form?.sourceMode === 'download' && form.autoDates;
  const automaticProposal = automaticDates ? currentPlan?.proposals.find(p=>p.mode==='auto_range') : null;
  const idempotencyRef = useRef<{ fingerprint: string; key: string } | null>(
    null,
  );
  const [announcement, setAnnouncement] = useState("");

  const [tasks, setTasks] = useState<ResearchTask[]>([]);
  const [tasksLoading, setTasksLoading] = useState(true);
  const [tasksError, setTasksError] = useState<string | null>(null);
  const [cancellingId, setCancellingId] = useState<string | null>(null);
  const taskSeqRef = useRef(0);
  const firstTasksLoadedRef = useRef(false);
  const notifiedTaskIdsRef = useRef<Set<string>>(new Set());
  const onResultsRef = useRef(onResultsChanged);
  onResultsRef.current = onResultsChanged;

  useEffect(() => {
    if (initializedRef.current || !options) return;
    initializedRef.current = true;
    const base = buildDefaultForm(options);
    if (isRecord(rawDraft)) {
      setForm(mergeDraft(base, rawDraft, options));
      setAnnouncement("已恢复上次保存的草稿，可继续编辑或提交。");
    } else {
      setForm(base);
    }
  }, [options, rawDraft]);

  useEffect(() => {
    if (!form) return;
    try {
      window.localStorage.setItem(DRAFT_KEY, JSON.stringify(form));
    } catch {
      // 本地存储不可用时仍允许正常提交。
    }
  }, [form]);

  const loadTasks = useCallback(async (silent = false) => {
    const seq = ++taskSeqRef.current;
    if (!silent) setTasksLoading(true);
    try {
      const response = await researchApi.listTasks();
      if (seq !== taskSeqRef.current) return;
      setTasks(response.items);
      setTasksError(null);
      const downloadedFailure = response.items.filter(task => task.status === 'failed' && task.request.data.mode === 'download' && task.progress.completed >= 1 && !notifiedTaskIdsRef.current.has(task.id));
      for (const task of downloadedFailure) notifiedTaskIdsRef.current.add(task.id);
      if (downloadedFailure.length) optionsState.reload();

      if (!firstTasksLoadedRef.current) {
        firstTasksLoadedRef.current = true;
        for (const task of response.items) {
          if (task.status === "succeeded") {
            notifiedTaskIdsRef.current.add(task.id);
          }
        }
      } else {
        let newlyCompleted = false;
        for (const task of response.items) {
          if (
            task.status === "succeeded" &&
            !notifiedTaskIdsRef.current.has(task.id)
          ) {
            notifiedTaskIdsRef.current.add(task.id);
            newlyCompleted = true;
          }
        }
        if (newlyCompleted) {
          onResultsRef.current();
          optionsState.reload();
        }
      }
    } catch (error) {
      if (seq !== taskSeqRef.current) return;
      setTasksError(describeResearchError(error));
    } finally {
      if (seq === taskSeqRef.current) setTasksLoading(false);
    }
  }, [optionsState.reload]);

  useEffect(() => {
    void loadTasks();
    return () => { taskSeqRef.current += 1; };
  }, [loadTasks]);

  const activeTaskCount = useMemo(
    () =>
      tasks.filter(
        (task) => task.status === "queued" || task.status === "running",
      ).length,
    [tasks],
  );

  useEffect(() => {
    if (activeTaskCount === 0) return;
    let alive = true;
    let timer: number;
    const poll = async () => {
      await loadTasks(true);
      if (alive) timer = window.setTimeout(poll, 1500);
    };
    timer = window.setTimeout(poll, 1500);
    return () => { alive = false; window.clearTimeout(timer); };
  }, [activeTaskCount, loadTasks]);

  const snapshotId = form?.snapshotId ?? "";
  const selectedSnapshot = useMemo(
    () =>
      options?.snapshots.find((item) => item.id === snapshotId) ?? null,
    [options, snapshotId],
  );
  const selectedCodeSet = useMemo(
    () => new Set(form?.selectedCodes ?? []),
    [form?.selectedCodes],
  );
  const poolCount = useMemo(() => {
    if (!form) return 0;
    return form.sourceMode==='index_warehouse' ? 300 : form.sourceMode === "snapshot"
      ? form.selectedCodes.length
      : unique(parseCodes(form.downloadCodes)).length;
  }, [form]);
  const factorWeightSum = useMemo(() => {
    if (!form) return Number.NaN;
    return Object.keys(form.factors).reduce(
      (sum, name) =>
        form.factors[name].enabled
          ? sum + toNumber(form.factors[name].weight)
          : sum,
      0,
    );
  }, [form]);
  const weightOk =
    Number.isFinite(factorWeightSum) &&
    Math.abs(factorWeightSum - 100) <= 1e-7;
  const readiness = form && options ? warmupInfo(form, options) : null;
  const reusableSnapshot = !automaticDates && !optionsState.loading && form && options ? matchingSnapshot(form, options) : null;

  function setField<K extends keyof FormState>(
    key: K,
    value: FormState[K],
  ) {
    setForm((current) =>
      current ? ({ ...current, [key]: value } as FormState) : current,
    );
  }

  function setFactor(name: FactorName, patch: Partial<FactorFormState>) {
    setForm((current) => {
      if (!current) return current;
      const nextFactors: Record<FactorName, FactorFormState> = {
        ...current.factors,
        [name]: { ...current.factors[name], ...patch },
      };
      return { ...current, factors: nextFactors };
    });
  }

  async function handleDatePlan() {
    if (!form || !options || planning) return;
    const planningForm = form.autoDates ? {...form,dataStart:form.btStart,dataEnd:form.btEnd} : form;
    const built=buildRequest(planningForm,options,{skipWarmup:true});
    if (!built.request) {setErrors(built.errors);return;}
    const key=dateKey;
    setPlanning(true);setPlanError(null);setDatePlan(null);setErrors([]);setSubmitError(null);
    try {
      const value=await researchApi.planDates({... (form.autoDates ? {auto_range:true as const} : {data_start:form.dataStart,data_end:form.dataEnd}),
        backtest_start:form.btStart,backtest_end:form.btEnd,factors:built.request.factors});
      setExtraCalendars(old=>[...old,...value.calendars]);
      if (dateKeyRef.current===key) setDatePlan({key,value});
      else setPlanError('查询期间日期或因子已变化，或其他配置已修改，请重新计算方案。');
    } catch(error) {setPlanError(describeResearchError(error));}
    finally {setPlanning(false);}
  }

  function duplicateFactor(key: string) {
    setForm(current => {
      if (!current) return current;
      const source = current.factors[key], name = source.name ?? key;
      let n = 2;
      while (current.factors[`${name}_${n}`]) n++;
      return {...current, factors:{...current.factors, [`${name}_${n}`]: {...source, name, enabled:true, weight:'0'}}};
    });
  }

  function removeFactor(key: string) {
    setForm(current => {
      if (!current) return current;
      const factors = {...current.factors}; delete factors[key];
      return {...current, factors};
    });
  }

  function handleSnapshotChange(id: string) {
    if (!form || !options) return;
    const snapshot =
      options.snapshots.find((item) => item.id === id) ?? null;
    if (!snapshot) return;
    const codes = snapshot.codes.map((item) => item.code);
    const clamped = clampTopk(form.topk, codes.length);
    const start = dateText(snapshot.start);
    const end = dateText(snapshot.end);
    const resetBacktest =
      !validDate(form.btStart) ||
      !validDate(form.btEnd) ||
      form.btStart < start ||
      form.btEnd > end;
    const suggestedStart = earliestBacktestDate(snapshot, form.factors);

    setForm({
      ...form,
      snapshotId: id,
      selectedCodes: codes,
      topk: clamped.topk,
      btStart: resetBacktest ? suggestedStart ?? "" : form.btStart,
      btEnd: resetBacktest ? end : form.btEnd,
    });
    setAnnouncement(
      [
        `已载入快照“${snapshot.label}”，股票池已更新。`,
        resetBacktest ? suggestedStart ? `回测开始日期已设为 ${suggestedStart}，为当前窗口留出预热数据。` : "该快照的历史数据不足以预热当前窗口，请缩短窗口并选择回测起点。" : "",
        clamped.clamped
          ? `目标持股数量 已调整为 ${clamped.topk}，不超过股票池数量。`
          : "",
      ]
        .filter(Boolean)
        .join(" "),
    );
  }

  function handleSourceMode(mode: SourceMode) {
    if (!form || mode === form.sourceMode) return;
    const availableSnapshots = options?.snapshots ?? [];
    setForm((current) => {
      if (!current) return current;
      if(mode==='index_warehouse' && options && historicalOption(options,current.warehouseId)) {
        const warehouse=historicalOption(options,current.warehouseId)!;
        return {...current,sourceMode:mode,warehouseId:warehouse.id,autoDates:false,
          btStart:current.btStart<warehouse.start || current.btStart>warehouse.end ? warehouse.start : current.btStart,
          btEnd:current.btEnd>warehouse.end || current.btEnd<warehouse.start ? warehouse.end : current.btEnd};
      }
      if (mode === "download" && (!current.dataStart || !current.dataEnd)) {
        return {
          ...current,
          sourceMode: mode,
          dataStart: current.dataStart || current.btStart,
          dataEnd: current.dataEnd || current.btEnd,
        };
      }
      if (
        mode === "snapshot" &&
        !current.snapshotId &&
        availableSnapshots.length
      ) {
        const snapshot = availableSnapshots[0];
        return {
          ...current,
          sourceMode: mode,
          snapshotId: snapshot.id,
          selectedCodes: snapshot.codes.map((item) => item.code),
        };
      }
      return { ...current, sourceMode: mode };
    });
    setErrors([]);
    setSubmitError(null);
  }

  function toggleCode(code: string) {
    if (!form) return;
    const exists = form.selectedCodes.includes(code);
    const next = exists
      ? form.selectedCodes.filter((item) => item !== code)
      : [...form.selectedCodes, code];
    const clamped = clampTopk(form.topk, next.length);
    setForm({
      ...form,
      selectedCodes: next,
      topk: clamped.topk,
    });
    if (clamped.clamped) {
      setAnnouncement(
        `目标持股数量 已调整为 ${clamped.topk}，不超过当前股票池数量。`,
      );
    }
  }

  function selectAllCodes() {
    if (!form || !selectedSnapshot) return;
    const codes = selectedSnapshot.codes.map((item) => item.code);
    const clamped = clampTopk(form.topk, codes.length);
    setForm({ ...form, selectedCodes: codes, topk: clamped.topk });
    if (clamped.clamped) {
      setAnnouncement(
        `目标持股数量 已调整为 ${clamped.topk}，不超过当前股票池数量。`,
      );
    }
  }

  function clearCodes() {
    if (!form) return;
    setForm({ ...form, selectedCodes: [] });
  }

  function applyEqualWeights() {
    if (!form) return;
    const enabled = Object.keys(form.factors).filter(
      (name) => form.factors[name].enabled,
    );
    if (!enabled.length) {
      setAnnouncement("请先勾选至少一个因子，再使用等权分配。");
      return;
    }
    const totalBasis = 10000;
    const base = Math.floor(totalBasis / enabled.length);
    let remainder = totalBasis - base * enabled.length;
    const nextFactors = { ...form.factors };
    for (const name of enabled) {
      const basis = base + (remainder > 0 ? 1 : 0);
      if (remainder > 0) remainder -= 1;
      nextFactors[name] = {
        ...nextFactors[name],
        weight: (basis / 100).toFixed(2),
      };
    }
    setForm({ ...form, factors: nextFactors });
    setAnnouncement("已将已启用因子按等权分配，权重合计 100%。");
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!form || !options || submitting) return;

    if (form.sourceMode === 'download' && form.autoDates) {
      await handleDatePlan();
      return;
    }

    const built = buildRequest(form, options);
    setErrors(built.errors);
    setSubmitError(null);
    if (!built.request) {
      setAnnouncement("请先修正表单中的问题再提交。");
      return;
    }

    await submitRequest(built.request);
  }

  async function confirmDownload() {
    if (!form || !options || !automaticProposal || submitting || planning) return;
    const p=automaticProposal;
    const built=buildRequest({...form,dataStart:p.data_start,dataEnd:p.data_end,
      btStart:p.backtest_start,btEnd:p.backtest_end},options);
    setErrors(built.errors);setSubmitError(null);
    if (built.request) await submitRequest(built.request);
  }

  async function submitRequest(request: ResearchRequest) {
    setSubmitting(true);
    try {
      const fingerprint = JSON.stringify(request);
      if (
        !idempotencyRef.current ||
        idempotencyRef.current.fingerprint !== fingerprint
      ) {
        idempotencyRef.current = {
          fingerprint,
          key: makeIdempotencyKey(),
        };
      }
      const task = await researchApi.createTask(
        request,
        idempotencyRef.current.key,
      );
      idempotencyRef.current = null;
      if (task.status === "succeeded" && !notifiedTaskIdsRef.current.has(task.id)) {
        notifiedTaskIdsRef.current.add(task.id);
        onResultsRef.current();
        optionsState.reload();
      }
      setTasks((current) => [
        task,
        ...current.filter((item) => item.id !== task.id),
      ]);
      setAnnouncement(
        `任务“${task.name}”已创建，正在排队运行；完成后可直接打开对应结果。`,
      );
      void loadTasks(true);
    } catch (error) {
      setSubmitError(describeResearchError(error));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleCancel(id: string) {
    setCancellingId(id);
    setTasksError(null);
    try {
      const task = await researchApi.cancelTask(id);
      setTasks((current) =>
        current.map((item) => (item.id === task.id ? task : item)),
      );
      setAnnouncement("已取消排队中的任务。");
      void loadTasks(true);
    } catch (error) {
      setTasksError(describeResearchError(error));
    } finally {
      setCancellingId(null);
    }
  }

  function handleReuse(task: ResearchTask) {
    if (!options) {
      setTasksError("研究配置尚未加载完成，暂时无法载入。");
      return;
    }
    const restored = formFromRequest(task.request, options);
    setForm(restored.form);
    setErrors([]);
    setSubmitError(null);
    setAnnouncement(
      restored.warning
        ? `已载入历史任务配置。${restored.warning}`
        : "已载入历史任务配置，可修改后重新提交。",
    );
    try {
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch {
      // 忽略滚动不可用的环境。
    }
  }

  function handleDownloadConfig(task: ResearchTask) {
    try {
      if (
        typeof URL === "undefined" ||
        typeof URL.createObjectURL !== "function"
      ) {
        throw new Error("unsupported");
      }
      const blob = new Blob([JSON.stringify(task.request, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `research-task-${task.id}.json`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch {
      setTasksError("无法生成配置 JSON 下载。");
    }
  }

  return (
    <div className="research-page">
      {optionsState.loading && !options ? (
        <section className="card">
          <LoadingPanel label="正在读取研究配置与可选快照…" />
        </section>
      ) : optionsState.error && !options ? (
        <section className="card">
          <ErrorPanel
            message={optionsState.error}
            onRetry={optionsState.reload}
          />
        </section>
      ) : form && options ? (
        <section className="card">
          <div className="card-header">
            <div>
              <h2 className="card-title">新建研究任务</h2>
              <p className="card-sub">
                配置数据来源、因子与回测参数，提交后在本地后端排队执行。
              </p>
            </div>
            <span className="badge">
              最多 {options.limits.max_queued_tasks} 个排队任务
            </span>
          </div>

          <form className="research-form" onSubmit={handleSubmit} noValidate>
            <div className="research-form-section">
              <div className="research-section-head">
                <h2 className="research-section-title">基础</h2>
                <span className="research-section-note">
                  任务在本地后端执行，结果保存后可在左侧页面查看。
                </span>
              </div>
              <div className="research-grid">
                <div className="field research-span-2">
                  <label htmlFor="research-name">实验名称</label>
                  <input
                    id="research-name"
                    type="text"
                    value={form.name}
                    maxLength={80}
                    placeholder="例如：动量与低波动组合"
                    onChange={(event) =>
                      setField("name", event.target.value)
                    }
                  />
                </div>
              </div>
            </div>

            <div className="research-form-section">
              <div className="research-section-head">
                <h2 className="research-section-title">数据来源</h2>
              </div>
              <fieldset className="toggle-field">
                <legend>股票池来源</legend>
                <div className="toggle-group">
                  <label htmlFor="research-source-snapshot">
                    <input
                      id="research-source-snapshot"
                      type="radio"
                      name="research-source"
                      checked={form.sourceMode === "snapshot"}
                      onChange={() => handleSourceMode("snapshot")}
                    />
                    使用已有数据
                  </label>
                  <label htmlFor="research-source-download">
                    <input
                      id="research-source-download"
                      type="radio"
                      name="research-source"
                      checked={form.sourceMode === "download"}
                      onChange={() => handleSourceMode("download")}
                    />
                    下载新股票池
                  </label>
                  <label htmlFor="research-source-historical">
                    <input id="research-source-historical" type="radio" name="research-source" checked={form.sourceMode==='index_warehouse'}
                      disabled={!historicalOption(options)} onChange={()=>handleSourceMode('index_warehouse')}/>
                    沪深300历史成分股
                  </label>
                </div>
              </fieldset>

              {form.sourceMode === "snapshot" ? (
                <div className="research-stack">
                  <div className="research-grid">
                    <div className="field">
                      <label htmlFor="research-snapshot">数据快照</label>
                      <select
                        id="research-snapshot"
                        value={form.snapshotId}
                        onChange={(event) =>
                          handleSnapshotChange(event.target.value)
                        }
                      >
                        <option value="">请选择快照</option>
                        {options.snapshots.map((snapshot) => (
                          <option key={snapshot.id} value={snapshot.id}>
                            {snapshot.label}（{snapshot.start} →{" "}
                            {snapshot.end}，{snapshot.trading_days} 交易日）
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>

                  {selectedSnapshot ? (
                    <>
                      <div className="research-toolbar">
                        <span>
                          已选 <strong>{form.selectedCodes.length}</strong> /{" "}
                          {selectedSnapshot.codes.length} 只
                        </span>
                        <span className="badge">
                          来源 {selectedSnapshot.source}
                        </span>
                        <span className="badge">
                          {selectedSnapshot.start} →{" "}
                          {selectedSnapshot.end}
                        </span>
                        <span className="badge">
                          校验{" "}
                          {VALIDATION_LABELS[
                            selectedSnapshot.validation_mode
                          ] ?? selectedSnapshot.validation_mode}
                        </span>
                        <span className="research-toolbar-spacer" />
                        <button
                          type="button"
                          className="text-button"
                          onClick={selectAllCodes}
                        >
                          全选
                        </button>
                        <button
                          type="button"
                          className="text-button"
                          onClick={clearCodes}
                        >
                          清空
                        </button>
                      </div>
                      <div
                        className="research-stock-list"
                        role="group"
                        aria-label="快照股票池"
                      >
                        {selectedSnapshot.codes.map((item) => (
                          <label className="research-check" key={item.code}>
                            <input
                              type="checkbox"
                              checked={selectedCodeSet.has(item.code)}
                              onChange={() => toggleCode(item.code)}
                            />
                            <span className="research-check-copy">
                              <span className="research-check-name">
                                {item.name}
                              </span>
                              <span className="research-check-code">
                                {item.code}
                              </span>
                            </span>
                          </label>
                        ))}
                      </div>
                      <p className="research-note">
                        使用本地已缓存快照，不会重新下载。股票池可任意选择子集，但至少保留 2 只。
                      </p>
                    </>
                  ) : (
                    <p className="research-note">
                      当前没有可用快照；可切换到“下载新股票池”。
                    </p>
                  )}
                </div>
              ) : form.sourceMode==='index_warehouse' ? (
                <div className="status-note" id="research-historical-info">
                  <label htmlFor="research-warehouse-version">历史成分数据版本</label>
                  <select id="research-warehouse-version" value={form.warehouseId} onChange={e=>setForm(current=>current?{...current,warehouseId:e.target.value}:current)}>
                    {(options.historical_indices??(options.historical_index?[options.historical_index]:[])).map(item=><option key={item.id} value={item.id}>{item.start} 至 {item.end} · {item.shared_version?`共享 ${item.shared_version}`:item.id}</option>)}
                  </select>
                  <p>使用已完成历史数据仓，按每日成分计算排名，含科创板；不会在回测中联网下载。</p>
                  <p>研究区间：{historicalOption(options,form.warehouseId)?.start} 至 {historicalOption(options,form.warehouseId)?.end}；预热数据起点：{historicalOption(options,form.warehouseId)?.data_start}。</p>
                  <p>{historicalOption(options,form.warehouseId)?.note}</p>
                  <p>调入前数据只用于预热；调出持仓持续尝试卖出，停牌或跌停时保留估值并记录退出受阻原因。历史不足、停牌或因子无效的股票不参与当日排名；候选不足不凑满持仓，可保留已有合规持仓与现金，并记录原因。</p>
                </div>
              ) : (
                <div className="research-stack">
                  <div className="field">
                    <label htmlFor="research-download-codes">
                      股票代码
                    </label>
                    <textarea
                      id="research-download-codes"
                      value={form.downloadCodes}
                      placeholder={
                        "000001.SZ, 600000.SH\n000002.SZ"
                      }
                      onChange={(event) =>
                        setField("downloadCodes", event.target.value)
                      }
                    />
                    <p className="research-note">
                      支持逗号、空格或换行分隔；当前识别 {poolCount}{" "}
                      个代码。重复或格式无效的代码会阻止提交。
                    </p>
                  </div>
                  <label className="research-note" htmlFor="research-auto-dates">
                    <input id="research-auto-dates" type="checkbox" checked={form.autoDates}
                      onChange={event=>setField('autoDates',event.target.checked)} />
                    按回测日期与因子窗口自动计算下载区间（推荐）
                  </label>
                  <div className="research-grid">
                    <div className="field">
                      <label htmlFor="research-data-start">
                        数据开始日期
                      </label>
                      <input
                        id="research-data-start"
                        type="date"
                        disabled={form.autoDates}
                        value={form.autoDates ? (automaticProposal?.data_start ?? '') : form.dataStart}
                        onChange={(event) =>
                          setField("dataStart", event.target.value)
                        }
                      />
                    </div>
                    <div className="field">
                      <label htmlFor="research-data-end">数据结束日期</label>
                      <input
                        id="research-data-end"
                        type="date"
                        disabled={form.autoDates}
                        value={form.autoDates ? (automaticProposal?.data_end ?? '') : form.dataEnd}
                        onChange={(event) =>
                          setField("dataEnd", event.target.value)
                        }
                      />
                    </div>
                    <div className="field">
                      <label htmlFor="research-benchmark">行情快照附带指数</label>
                      <select
                        id="research-benchmark"
                        value={form.benchmark}
                        onChange={(event) =>
                          setField("benchmark", event.target.value)
                        }
                      >
                        {form.benchmark &&
                        !options.benchmarks.some(
                          (item) => item.code === form.benchmark,
                        ) ? (
                          <option value={form.benchmark}>
                            {form.benchmark}
                          </option>
                        ) : null}
                        {options.benchmarks.map((item) => (
                          <option key={item.code} value={item.code}>
                            {item.name}（{item.code}）
                          </option>
                        ))}
                      </select>
                    </div>
                    <div className="field">
                      <label htmlFor="research-validation-mode">
                        数据校验模式
                      </label>
                      <select
                        id="research-validation-mode"
                        value={form.validationMode}
                        onChange={(event) =>
                          setField(
                            "validationMode",
                            event.target.value as ValidationMode,
                          )
                        }
                      >
                        {VALIDATION_OPTIONS.map((item) => (
                          <option key={item.value} value={item.value}>
                            {item.label}
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>
                  <p className="research-note">
                    {form.autoDates ? '只需设置下方回测日期与因子，点击“预览下载方案”后查看自动推算的区间，再确认下载。预览仅查询交易日历。' : '新下载使用后端已配置的数据服务。数据区间需覆盖回测区间，并留出因子计算所需的历史数据。'}
                  </p>
                  {reusableSnapshot ? <div className="status-note">
                    <p>找到相同数据区间、基准与校验模式的已有快照，可覆盖当前股票池：{reusableSnapshot.id}。</p>
                    <button type="button" id="research-use-cached-snapshot" className="text-button" onClick={() => {
                      setForm({...form,sourceMode:'snapshot',snapshotId:reusableSnapshot.id,selectedCodes:unique(parseCodes(form.downloadCodes))});
                      setErrors([]); setSubmitError(null);
                      setAnnouncement('已改用已有快照，保留当前股票池、因子、窗口、权重和回测日期。');
                    }}>改用已有快照（免下载）</button>
                  </div> : null}
                </div>
              )}
            </div>

            <div className="research-form-section">
              <div className="research-section-head">
                <h2 className="research-section-title">因子与权重</h2>
                <span className="research-section-note">
                  权重按百分比填写，已启用因子合计需为 100%。
                </span>
              </div>
              <div className="research-factor-grid">
                <div className="field" style={{marginBottom:16,gridColumn:'1 / -1'}}>
                  <label htmlFor="research-neutralization">行业中性化</label>
                  <select id="research-neutralization" value={form.neutralizationMode} onChange={e=>setField('neutralizationMode',e.target.value as 'off'|'industry')}>
                    <option value="off">关闭（沿用原始因子评分）</option>
                    <option value="industry" disabled={!options.industry_data}>开启（按当日历史申万一级行业）</option>
                  </select>
                  <p className="hint-text">开启后：方向统一 → 当日截面缩尾 → 减行业均值 → 标准化残差 → 百分位评分。保存处理前后明细；缺失或冲突行业不参与评分，单股票行业残差为0。此功能不限制持仓行业权重。</p>
                  <p className="hint-text">{options.industry_data ? `已下载${options.industry_data.instruments}只股票的历史行业，核验截至${options.industry_data.data_asof ?? '未记录'}；当前配置版本${form.industryVersion.slice(0,12) || '待固定'}。新旧分类分段使用，实际公告时点未认证。` : '历史行业数据尚未准备，暂不能开启。'}</p>
                  {options.industry_data && form.industryVersion!==options.industry_data.id ? <button type="button" className="text-button" onClick={()=>setField('industryVersion',options.industry_data!.id)}>使用最新行业数据</button>:null}
                </div>
                {Object.entries(form.factors).map(([name, factor]) => {
                  const formulaName = factor.name ?? name;
                  const definition =
                    options.factor_definitions.find(
                      (item) => item.name === formulaName,
                    ) ?? null;
                  const needsWindow = definition?.default_window != null;
                  return (
                    <div
                      key={name}
                      className={`research-factor-card${
                        factor.enabled ? " is-active" : ""
                      }`}
                    >
                      <label
                        className="research-factor-head"
                        htmlFor={`research-factor-${name}`}
                      >
                        <input
                          id={`research-factor-${name}`}
                          type="checkbox"
                          checked={factor.enabled}
                          onChange={(event) =>
                            setFactor(name, {
                              enabled: event.target.checked,
                            })
                          }
                        />
                        <span className="research-factor-copy">
                          <span className="research-factor-title">
                            {definition?.label ?? factorLabel(formulaName)}{name !== formulaName ? ` [${name}]` : ''}
                          </span>
                          <span className="research-factor-desc">
                            {definition?.description ?? ""}
                          </span>
                        </span>
                      </label>
                      <p className="research-note">{definition?.family ? `类别：${definition.family_label ?? definition.family} · ` : ''}{definition?.direction === 'lower' ? '原始值越低越好' : '原始值越高越好'}{definition?.required_fields?.length ? ` · 字段：${definition.required_fields.join('、')}` : ''}</p>
                      {definition?.data_note ? <p className="research-note">{definition.data_note}</p> : null}
                      <div className="research-factor-body">
                        <div className="field">
                          <label htmlFor={`research-factor-weight-${name}`}>
                            权重（%）
                          </label>
                          <input
                            id={`research-factor-weight-${name}`}
                            type="number"
                            min="0"
                            max="100"
                            step="0.01"
                            value={factor.weight}
                            disabled={!factor.enabled}
                            onChange={(event) =>
                              setFactor(name, {
                                weight: event.target.value,
                              })
                            }
                          />
                        </div>
                        {needsWindow ? (
                          <div className="field">
                            <label htmlFor={`research-factor-window-${name}`}>
                              窗口／回看总跨度（交易日）
                            </label>
                            <input
                              id={`research-factor-window-${name}`}
                              type="number"
                              min="2"
                              max="252"
                              step="1"
                              value={factor.window}
                              disabled={!factor.enabled}
                              onChange={(event) =>
                                setFactor(name, {
                                  window: event.target.value,
                                })
                              }
                            />
                          </div>
                        ) : (
                          <div className="field">
                            <span className="research-static-label">
                              窗口
                            </span>
                            <span className="research-static-value">
                              无窗口参数
                            </span>
                          </div>
                        )}
                        {definition?.default_skip != null ? <div className="field">
                          <label htmlFor={`research-factor-skip-${name}`}>剔除最近天数</label>
                          <input id={`research-factor-skip-${name}`} type="number" min="0" max="251" step="1" value={factor.skip ?? String(definition.default_skip)} disabled={!factor.enabled} onChange={e => setFactor(name,{skip:e.target.value})} />
                        </div> : null}
                        {definition?.default_baseline_window != null ? <div className="field">
                          <label htmlFor={`research-factor-baseline-${name}`}>此前基准窗口（不重叠）</label>
                          <input id={`research-factor-baseline-${name}`} type="number" min="2" max="252" step="1" value={factor.baselineWindow ?? String(definition.default_baseline_window)} disabled={!factor.enabled} onChange={e => setFactor(name,{baselineWindow:e.target.value})} />
                        </div> : null}
                      </div>
                      <div className="research-task-actions">
                        {needsWindow ? <button type="button" className="text-button" onClick={() => duplicateFactor(name)}>添加同类不同窗口</button> : null}
                        {name !== formulaName ? <button type="button" className="text-button" onClick={() => removeFactor(name)}>移除此实例</button> : null}
                      </div>
                    </div>
                  );
                })}
              </div>
              <div className="research-weight-summary">
                <span
                  className={`research-weight-total${
                    weightOk ? " is-ok" : " is-bad"
                  }`}
                >
                  权重合计{" "}
                  {Number.isFinite(factorWeightSum)
                    ? Number(factorWeightSum.toFixed(2))
                    : "—"}
                  %
                </span>
                <button
                  type="button"
                  className="text-button"
                  onClick={applyEqualWeights}
                >
                  等权分配
                </button>
                <span className="research-section-note">
                  勾选或取消因子不会自动改动其他权重。
                </span>
              </div>
            </div>

            <div className="research-form-section">
              <div className="research-section-head">
                <h2 className="research-section-title">回测基础</h2>
                <span className="research-section-note">
                  当前股票池 {poolCount} 只
                </span>
              </div>
              <div className="field">
                <label htmlFor="research-comparison-benchmark">评价基准</label>
                <select id="research-comparison-benchmark" value={form.comparisonBenchmark}
                  onChange={event=>setField('comparisonBenchmark',event.target.value)}>
                  <option value="">沿用数据集原基准（兼容旧配置）</option>
                  {(options.comparison_benchmarks ?? options.benchmarks).map(item=><option key={item.code} value={item.code}>{item.name}（{item.code}）</option>)}
                </select>
                <p className="research-note">评价基准独立于选股与策略执行；切换后复用相同成交、持仓和策略净值，仅重算基准与相对绩效。全收益指数包含分红再投资。</p>
                {(() => {const choice=options.comparison_benchmarks?.find(item=>item.code===form.comparisonBenchmark);return choice?.coverage ? <p className="research-note">官方Tushare本地覆盖：{choice.coverage.start} 至 {choice.coverage.end}，{choice.coverage.rows}条。缺少所选区间时，任务将从官方补取并校验。</p> : form.comparisonBenchmark ? <p className="research-note">所选基准将在任务中从官方Tushare下载并校验；获取失败会明确报错。</p> : null;})()}
              </div>
              {readiness && !automaticDates ? <div id="research-warmup" className={readiness.problem ? 'warning-callout' : 'status-note'}>
                <p>{readiness.problem ?? `当前因子至少需要${readiness.required}个回测前历史交易日。${readiness.known ? `当前已有${readiness.available}个，满足窗口长度要求。` : '暂无覆盖此区间的本地交易日历，下载后校验实际交易日数量。'}`}</p>
                <p className="hint-text">预热数据用于回测开始前的因子计算；估值缺失、停牌和负收益样本仍需在准备数据时检查。</p>
                {form.sourceMode==='download' ? <div>
                  <p className="hint-text">可查询交易日历，计算补足历史或延后回测的方案；本操作不下载股票行情。</p>
                  <button type="button" id="research-plan-dates" className="text-button" disabled={planning} onClick={()=>void handleDatePlan()}>{planning ? '正在查询交易日历…' : '计算日期调整方案'}</button>
                  {planError ? <p role="alert">{planError}</p> : null}
                  {currentPlan ? <div id="research-date-proposals">
                    {currentPlan.first_trading_day!==currentPlan.requested_backtest_start ? <p>所选回测起点非交易日，方案将顺延至{currentPlan.first_trading_day}。</p> : null}
                    {currentPlan.proposals.filter(p=>p.mode!=='auto_range').map(p=><div key={p.mode}>
                      <p>{p.mode==='extend_history' ? '补足历史，保留回测年份' : '保留数据起点，延后回测'}：下载{p.data_start}至{p.data_end}；回测{p.backtest_start}至{p.backtest_end}；已验证至少{p.history_days}个历史交易日。</p>
                      {p.mode==='extend_history' && p.data_start<form.dataStart ? <p className="hint-text">此方案扩大历史行情区间；应用后需点击“创建研究任务”才开始下载。</p> : null}
                      <button type="button" id={`research-apply-${p.mode}`} className="text-button" onClick={()=>{
                        setForm({...form,dataStart:p.data_start,dataEnd:p.data_end,btStart:p.backtest_start,btEnd:p.backtest_end});
                        setErrors([]);setSubmitError(null);setPlanError(null);setDatePlan(null);
                        setAnnouncement('已应用日期方案，股票池、因子窗口与权重保持原值；请确认后创建任务。');
                      }}>{p.mode==='extend_history' ? '应用：补足预热历史' : '应用：延后回测起点'}</button>
                    </div>)}
                  </div> : null}
                </div> : null}
                {readiness.problem && readiness.earliest && readiness.earliest < form.btEnd ? <button
                  type="button" id="research-use-warmup-start" className="text-button"
                  onClick={() => {
                    setField('btStart',readiness.earliest!); setErrors([]); setSubmitError(null);
                    setAnnouncement(`回测起点已设为${readiness.earliest}，因子窗口与权重保持原值。`);
                  }}>将回测起点设为{readiness.earliest}</button> : null}
              </div> : null}
              <div className="research-grid">
                <div className="field">
                  <label htmlFor="research-bt-start">回测开始日期</label>
                  <input
                    id="research-bt-start"
                    type="date"
                    value={form.btStart}
                    onChange={(event) =>
                      setField("btStart", event.target.value)
                    }
                  />
                </div>
                <div className="field">
                  <label htmlFor="research-bt-end">回测结束日期</label>
                  <input
                    id="research-bt-end"
                    type="date"
                    value={form.btEnd}
                    onChange={(event) =>
                      setField("btEnd", event.target.value)
                    }
                  />
                </div>
                <div className="field">
                  <label htmlFor="research-topk">目标持股数量</label>
                  <input
                    id="research-topk"
                    type="number"
                    min="1"
                    step="1"
                    value={form.topk}
                    onChange={(event) =>
                      setField("topk", event.target.value)
                    }
                  />
                </div>
                <div className="field">
                  <label htmlFor="research-initial-cash">
                    初始资金（元）
                  </label>
                  <input
                    id="research-initial-cash"
                    type="number"
                    min="0.01"
                    step="0.01"
                    value={form.initialCash}
                    onChange={(event) =>
                      setField("initialCash", event.target.value)
                    }
                  />
                </div>
              </div>
              <p className="research-note">
                {automaticDates ? '预热区间将按所选因子自动补足。非交易日开始将顺延、结束将前移，调整结果会在下载前展示。' : '回测区间需位于数据覆盖范围内；目标持股数量不能超过当前股票池数量。因子窗口回看需要额外历史数据，请为回测起点预留前置区间。'}
              </p>
              <div className="field">
                <label htmlFor="research-rebalance-frequency">调仓频率</label>
                <select id="research-rebalance-frequency" value={form.rebalanceFrequency} onChange={event=>setField('rebalanceFrequency',event.target.value as FormState['rebalanceFrequency'])}>
                  <option value="daily">每日</option><option value="weekly">每周首个交易日</option><option value="monthly">每月首个交易日</option>
                </select>
                <p className="hint-text">区间首日建仓，调仓日使用前一交易日评分；每日计算净值。历史池成分调出仍每日尝试退出，非调仓日卖出后的现金等下一调仓日补仓。</p>
              </div>
            </div>

            <div className="research-form-section">
              <div className="research-advanced-head">
                <div>
                  <h2 className="research-section-title">高级设置</h2>
                  <p className="research-section-note">
                    交易成本、仓位比例与因子诊断参数
                  </p>
                </div>
                <button
                  type="button"
                  className="button button-compact"
                  aria-expanded={form.advancedOpen}
                  aria-controls="research-advanced"
                  onClick={() =>
                    setField("advancedOpen", !form.advancedOpen)
                  }
                >
                  {form.advancedOpen ? "收起高级设置" : "展开高级设置"}
                </button>
              </div>

              {form.advancedOpen ? (
                <div
                  id="research-advanced"
                  className="research-advanced-body research-grid research-grid-wide"
                >
                  <div className="field">
                    <label htmlFor="research-n-drop">每次最多替换数量</label>
                    <input
                      id="research-n-drop"
                      type="number"
                      min="0"
                      step="1"
                      value={form.nDrop}
                      onChange={(event) =>
                        setField("nDrop", event.target.value)
                      }
                    />
                  </div>
                  <div className="field">
                    <label htmlFor="research-risk-degree">
                      目标股票仓位（%）
                    </label>
                    <input
                      id="research-risk-degree"
                      type="number"
                      min="0.01"
                      max="100"
                      step="0.01"
                      value={form.riskDegree}
                      onChange={(event) =>
                        setField("riskDegree", event.target.value)
                      }
                    />
                  </div>
                  <div className="field">
                    <label htmlFor="research-cost-mode">费用模式</label>
                    <select id="research-cost-mode" value={form.costMode} onChange={event=>setField('costMode',event.target.value as FormState['costMode'])}>
                      <option value="legacy">旧版综合费率（兼容已有配置）</option><option value="itemized">分项：佣金、印花税、过户费</option>
                    </select>
                  </div>
                  {form.costMode==='legacy' ? <>
                  <div className="field">
                    <label htmlFor="research-open-cost">
                      买入综合费率（%）
                    </label>
                    <input
                      id="research-open-cost"
                      type="number"
                      min="0"
                      step="0.0001"
                      value={form.openCost}
                      onChange={(event) =>
                        setField("openCost", event.target.value)
                      }
                    />
                  </div>
                  <div className="field">
                    <label htmlFor="research-close-cost">
                      卖出综合费率（%）
                    </label>
                    <input
                      id="research-close-cost"
                      type="number"
                      min="0"
                      step="0.0001"
                      value={form.closeCost}
                      onChange={(event) =>
                        setField("closeCost", event.target.value)
                      }
                    />
                  </div>
                  </> : <>
                    <div className="field"><label htmlFor="research-commission-rate">券商综合佣金（双边，%）</label>
                      <input id="research-commission-rate" type="number" min="0" step="0.0001" value={form.commissionRate} onChange={event=>setField('commissionRate',event.target.value)}/>
                    </div>
                    <div className="field"><label htmlFor="research-tax-mode">印花税与过户费</label>
                      <select id="research-tax-mode" value={form.taxMode} onChange={event=>setField('taxMode',event.target.value as FormState['taxMode'])}>
                        <option value="historical">按成交日历史费率</option><option value="fixed">自定义固定费率</option>
                      </select>
                    </div>
                    {form.taxMode==='fixed' ? <>
                      <div className="field"><label htmlFor="research-stamp-tax-rate">卖出印花税（%）</label>
                        <input id="research-stamp-tax-rate" type="number" min="0" step="0.0001" value={form.stampTaxRate} onChange={event=>setField('stampTaxRate',event.target.value)}/>
                      </div>
                      <div className="field"><label htmlFor="research-transfer-fee-rate">双边过户费（%）</label>
                        <input id="research-transfer-fee-rate" type="number" min="0" step="0.0001" value={form.transferFeeRate} onChange={event=>setField('transferFeeRate',event.target.value)}/>
                      </div>
                    </> : null}
                  </>}
                  <div className="field"><label htmlFor="research-impact-cost">双边冲击成本（成交额比例，%）</label>
                    <input id="research-impact-cost" type="number" min="0" step="0.001" value={form.impactCost} onChange={event=>setField('impactCost',event.target.value)}/>
                  </div>
                  <div className="field">
                    <label htmlFor="research-min-cost">
                      {form.costMode==='itemized' ? '最低单笔佣金（元）' : '最低单笔综合费用（元）'}
                    </label>
                    <input
                      id="research-min-cost"
                      type="number"
                      min="0"
                      step="0.01"
                      value={form.minCost}
                      onChange={(event) =>
                        setField("minCost", event.target.value)
                      }
                    />
                  </div>
                  <div className="field">
                    <label htmlFor="research-horizons">
                      诊断持有期（交易日）
                    </label>
                    <input
                      id="research-horizons"
                      type="text"
                      value={form.horizons}
                      placeholder="1, 5, 20"
                      onChange={(event) =>
                        setField("horizons", event.target.value)
                      }
                    />
                  </div>
                  <div className="field">
                    <label htmlFor="research-groups">诊断分组数</label>
                    <input
                      id="research-groups"
                      type="number"
                      min="2"
                      max="10"
                      step="1"
                      value={form.groups}
                      onChange={(event) =>
                        setField("groups", event.target.value)
                      }
                    />
                  </div>
                  <div className="field">
                    <label htmlFor="research-min-cross">
                      计算 IC 所需的最少股票数
                    </label>
                    <input
                      id="research-min-cross"
                      type="number"
                      min="3"
                      step="1"
                      value={form.minCrossSection}
                      onChange={(event) =>
                        setField("minCrossSection", event.target.value)
                      }
                    />
                  </div>
                </div>
              ) : null}
              <p className="research-note">
                {form.costMode==='itemized' ? '分项模式：最低金额只适用于佣金，印花税仅卖出收取，过户费与冲击成本另加；旧版综合费率不重复收取。佣金按含经手等规费、不含印花税与过户费填写；已含过户费可选择自定义并将其设为0。' : '旧版模式沿用买卖综合费率与最低费用，不额外叠加佣金、印花税或过户费；冲击成本另加。'}
                冲击成本是双边成交额比例扣费的压力假设，不是容量预测，也不使用开盘后才知道的当日总成交量。费率以百分数填写，0.03%即万3。诊断持有期仅用于因子评估，不设置调仓周期。
              </p>
            </div>

            {automaticDates ? <div id="research-automatic-plan" className="status-note">
              <p>预热数据只用于回测前计算因子，不计入策略收益。预览会查询交易日历，确认后才下载行情并运行任务。</p>
              {planError ? <p role="alert">{planError}</p> : null}
              {automaticProposal ? <div id="research-download-confirmation">
                <h3>确认本次下载与回测</h3>
                <p>预热：{automaticProposal.warmup_start} 至 {automaticProposal.warmup_end}（{automaticProposal.history_days} 个交易日）</p>
                <p>回测：{automaticProposal.backtest_start} 至 {automaticProposal.backtest_end}</p>
                {(automaticProposal.backtest_start!==form.btStart || automaticProposal.backtest_end!==form.btEnd) ?
                  <p>所选日期含非交易日，已按开市日调整，请确认以上实际回测区间。</p> : null}
                <p>下载完整区间：{automaticProposal.data_start} 至 {automaticProposal.data_end}；股票池 {poolCount} 只；基准 {form.benchmark}。</p>
                <p>初始资金 {form.initialCash} 元；目标持股 {form.topk} 只；数据校验：{VALIDATION_LABELS[form.validationMode]}。</p>
                <details><summary>查看股票与因子参数</summary>
                  <p>{parseCodes(form.downloadCodes).join('、')}</p>
                  {Object.entries(form.factors).filter(([,f])=>f.enabled).map(([key,f])=><p key={key}>
                    {f.definition?.label ?? f.name ?? key} [{key}]：权重 {f.weight}%
                    {f.window ? `；窗口 ${f.window} 日` : ''}{f.skip ? `；跳过 ${f.skip} 日` : ''}{f.baselineWindow ? `；基准窗口 ${f.baselineWindow} 日` : ''}
                  </p>)}
                </details>
                <button id="research-confirm-download" type="button" className="button button-primary"
                  disabled={submitting || planning} onClick={()=>void confirmDownload()}>{submitting ? '提交中…' : '确认下载并运行'}</button>
              </div> : <p>设置好回测日期与因子后，点击下方“预览下载方案”。修改配置后需重新预览。</p>}
            </div> : null}

            {errors.length ? (
              <div className="research-error-box" role="alert">
                <div className="research-error-title">请修正以下问题</div>
                <ul className="research-error-list">
                  {errors.map((item, index) => (
                    <li key={`${item}-${index}`}>{item}</li>
                  ))}
                </ul>
              </div>
            ) : null}

            {submitError ? (
              <div className="research-error-box" role="alert">
                <div className="research-error-title">提交失败</div>
                <p>{submitError}</p>
              </div>
            ) : null}

            {announcement ? (
              <p className="research-announcement" role="status">
                {announcement}
              </p>
            ) : null}

            <div className="research-submit-row">
              <button
                type="submit"
                className="button button-primary"
                disabled={submitting || planning}
              >
                {planning ? '正在查询交易日历…' : submitting ? "提交中…" : automaticDates ? "预览下载方案" : "创建研究任务"}
              </button>
              <span className="research-note">
                提交后会自动轮询排队与运行状态，完成时可打开本次任务对应的评分、回测与因子评估。
              </span>
            </div>
          </form>
        </section>
      ) : null}

      <section className="card">
        <div className="card-header">
          <div>
            <h2 className="card-title">研究任务</h2>
            <p className="card-sub">
              排队与运行中的任务每 1.5 秒自动刷新。失败、中断或缺少结果不会被当作 0 收益。
            </p>
          </div>
          <div className="card-actions">
            {activeTaskCount ? (
              <span className="badge">{activeTaskCount} 个进行中</span>
            ) : null}
            <button
              type="button"
              className="button button-compact"
              onClick={() => void loadTasks()}
              disabled={tasksLoading}
            >
              {tasksLoading ? "刷新中…" : "刷新"}
            </button>
          </div>
        </div>

        {tasksLoading && !tasks.length ? (
          <LoadingPanel label="正在读取研究任务…" />
        ) : null}

        {tasksError ? (
          <div className="research-error-box" role="alert">
            <div className="research-error-title">任务操作失败</div>
            <p>{tasksError}</p>
          </div>
        ) : null}

        {!tasksLoading && !tasks.length && !tasksError ? (
          <EmptyPanel title="暂无研究任务">
            提交配置后，任务进度与完成结果会显示在这里。
          </EmptyPanel>
        ) : null}

        {tasks.length ? (
          <div className="research-task-list">
            {tasks.map((task) => (
              <TaskCard
                key={task.id}
                task={task}
                canReuse={Boolean(options) && !optionsState.loading}
                cancelling={cancellingId === task.id}
                onCancel={handleCancel}
                onOpenResult={onOpenResult}
                onReuse={handleReuse}
                onDownload={handleDownloadConfig}
              />
            ))}
          </div>
        ) : null}
      </section>
    </div>
  );
}
