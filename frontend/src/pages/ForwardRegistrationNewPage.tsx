import { Button, Input } from "antd";
import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import {
  forwardRegistrationsApi,
  type ForwardRegistrationRequest,
  type ForwardSource,
} from "../api/forwardRegistrations";
import { navigate, paths } from "../app/routes";
import { PageHeader, Section } from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import { useForwardRegistrationSubmission } from "../hooks/useForwardRegistrationSubmission";
import "./forwardRegistration.css";
import "./forwardRegistrationCreate.css";

const SOURCE_PAGE_SIZE = 6;
const STOCK_LIMIT = 50;
const STOCK_CODE = /^[0-9]{6}\.(?:SH|SZ|BJ)$/;

type FieldName =
  | "source"
  | "name"
  | "signal"
  | "pool"
  | "window"
  | "topK"
  | "direction";
type Problems = Partial<Record<FieldName, string>>;

function rejectedField(detail: string): FieldName | undefined {
  if (detail.includes("来源数据集")) return "source";
  if (/信号日|交易日历|00:00/.test(detail)) return "signal";
  if (detail.includes("股票池")) return "pool";
  if (/前 K|前K/.test(detail)) return "topK";
  if (detail.includes("动量窗口")) return "window";
  if (detail.includes("登记名称")) return "name";
}

function Field({
  field,
  label,
  problem,
  hint,
  children,
}: {
  field: FieldName;
  label: string;
  problem?: string;
  hint?: string;
  children: (props: {
    id: string;
    "aria-invalid": boolean;
    "aria-describedby"?: string;
  }) => ReactNode;
}) {
  const id = "forward-create-" + field;
  return (
    <div className="forward-create-field">
      <label htmlFor={id}>{label}</label>
      {children({
        id,
        "aria-invalid": Boolean(problem),
        "aria-describedby":
          [hint ? id + "-hint" : "", problem ? id + "-error" : ""]
            .filter(Boolean)
            .join(" ") || undefined,
      })}
      {hint && (
        <p id={id + "-hint"} className="forward-create-hint">
          {hint}
        </p>
      )}
      {problem && (
        <p id={id + "-error"} className="forward-create-error">
          {problem}
        </p>
      )}
    </div>
  );
}

export default function ForwardRegistrationNewPage() {
  const [sourceOffset, setSourceOffset] = useState(0);
  const sources = useApiResource(
    (signal) =>
      forwardRegistrationsApi.sources(sourceOffset, SOURCE_PAGE_SIZE, signal),
    [sourceOffset],
  );
  const [chosen, setChosen] = useState<ForwardSource>();
  const selection = useApiResource(
    chosen
      ? (signal) => forwardRegistrationsApi.source(chosen.id, signal)
      : null,
    [chosen?.id],
  );
  const [name, setName] = useState("");
  const [signalDate, setSignalDate] = useState("");
  const [pool, setPool] = useState("");
  const [window, setWindow] = useState("20");
  const [topK, setTopK] = useState("1");
  const [direction, setDirection] =
    useState<ForwardRegistrationRequest["factor_direction"]>("high");
  const [checked, setChecked] = useState(false);
  const heading = useRef<HTMLElement>(null);
  const message = useRef<HTMLDivElement>(null);
  const { state, submit, retry, clearRejection } =
    useForwardRegistrationSubmission((id) =>
      navigate(paths.forwardRegistration(id), {
        replace: true,
        focusHeading: true,
      }),
    );
  const waiting = state.kind === "waiting";
  const unconfirmed = state.kind === "unconfirmed";
  const locked = waiting || unconfirmed;

  const items = sources.data?.items;
  const sourceLost = selection.errorStatus === 404;
  const selectedVisible = items?.some((item) => item.id === chosen?.id);
  const selectedOption = selection.data ?? chosen;
  const entries = pool
    .split(/[\s,，;；、]+/)
    .filter(Boolean)
    .map((code) => code.toUpperCase());
  const valid = entries.filter((code) => STOCK_CODE.test(code));
  const unique = [...new Set(valid)].sort();
  const duplicateCount = valid.length - unique.length;
  const invalidCount = entries.length - valid.length;
  const issues: Problems = {};
  if (!chosen || sourceLost) issues.source = "请选择可用受管来源。";
  if (!name.trim()) issues.name = "请输入登记名称。";
  if (
    !/^\d{4}-\d{2}-\d{2}$/.test(signalDate) ||
    signalDate.startsWith("0000")
  ) {
    issues.signal = "请输入有效的信号日。";
  }
  if (
    duplicateCount ||
    invalidCount ||
    unique.length < 2 ||
    unique.length > STOCK_LIMIT
  ) {
    issues.pool = "请输入 2–50 只有效且不重复的 A 股代码。";
  }
  if (
    !/^\d+$/.test(topK) ||
    Number(topK) < 1 ||
    Number(topK) >= unique.length
  ) {
    issues.topK = "前 K 必须是大于 0 且小于有效股票数的整数。";
  }
  if (!/^\d+$/.test(window) || Number(window) < 2 || Number(window) > 252) {
    issues.window = "动量窗口必须是 2–252 的整数。";
  }
  const localError = checked && Object.keys(issues).length > 0;
  const rejection = state.kind === "rejected" ? state.detail : undefined;
  const problemField = rejection && rejectedField(rejection);
  const canConfirm =
    !sources.loading &&
    !sources.refreshing &&
    !sources.error &&
    (chosen
      ? selection.data?.id === chosen.id &&
        !selection.error &&
        !selection.refreshing
      : Boolean(items?.length));

  useEffect(() => {
    heading.current?.focus();
  }, []);
  useEffect(() => {
    if (unconfirmed || rejection || localError) message.current?.focus();
  }, [unconfirmed, rejection, localError]);

  function formSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (locked || !canConfirm) return;
    if (Object.keys(issues).length || !chosen) {
      if (checked) message.current?.focus();
      setChecked(true);
      return;
    }
    setChecked(false);
    submit({
      name: name.trim(),
      signal_date: signalDate,
      pool_codes: unique,
      top_k: Number(topK),
      factor_window: Number(window),
      factor_direction: direction,
      dataset_id: chosen.id,
    });
  }

  function problem(field: FieldName) {
    return field === problemField
      ? rejection
      : checked
        ? issues[field]
        : undefined;
  }

  function refreshSources() {
    sources.reload();
    if (chosen) selection.reload();
  }

  function changeSourcePage(offset: number) {
    setSourceOffset(offset);
    if (chosen) selection.reload();
  }

  function removeStock(position: number) {
    setPool(entries.filter((_, index) => index !== position).join(" "));
    document.getElementById("forward-create-pool")?.focus();
    clearRejection();
  }

  return (
    <div className="forward-page forward-create">
      <PageHeader
        title="新建前瞻登记"
        headingRef={heading}
        back={{ to: paths.forwardRegistrations(), label: "返回登记列表" }}
      />
      <form noValidate onSubmit={formSubmit} onChange={clearRejection}>
        {(waiting || unconfirmed || rejection || localError) && (
          <div
            ref={message}
            tabIndex={-1}
            className="forward-create-feedback"
            role={waiting ? "status" : "alert"}
          >
            {waiting ? (
              <strong>正在确认登记</strong>
            ) : unconfirmed ? (
              <>
                <p>尚未确认登记是否成功。原参数已保留，可按原参数重试。</p>
                {state.detail && <p>{state.detail}</p>}
                <Button type="primary" onClick={retry}>
                  按原参数重试
                </Button>
              </>
            ) : (
              <>
                <strong>
                  {rejection ? "登记被拒绝" : "请检查表单中的字段"}
                </strong>
                {rejection && <p>{rejection}</p>}
              </>
            )}
          </div>
        )}

        <Section title={<h2 id="forward-source-title">受管来源</h2>}>
          <p className="forward-create-hint">
            行情覆盖区间供选源参考。提交时由服务端复核受管清单、交易日历和预热。
          </p>
          {sources.loading ? (
            <p role="status">正在加载受管来源</p>
          ) : sources.error ? (
            <p role="alert">受管来源读取失败。{sources.error}</p>
          ) : items?.length === 0 && !chosen ? (
            <p role="status">
              {sources.data?.has_more || sourceOffset > 0
                ? "本页暂无可用受管来源"
                : "暂无可用受管来源"}
            </p>
          ) : null}
          {selectedOption && (!selectedVisible || sourceLost) && (
            <p>
              已选来源 {selectedOption.name} · 尾号{" "}
              {selectedOption.id.slice(-8)}
            </p>
          )}
          {sourceLost && (
            <p role={locked ? "status" : "alert"}>
              {locked
                ? "原受管来源已不可用，仍保留原参数供重试。"
                : "所选来源已不可用，请重新选择。"}
            </p>
          )}
          {selection.error && !sourceLost && !sources.error && (
            <p role="alert">暂时无法复核所选来源。{selection.error}</p>
          )}
          {problem("source") && (
            <p id="forward-source-error" className="forward-create-error">
              {problem("source")}
            </p>
          )}
          {items && items.length > 0 && (
            <fieldset
              disabled={locked || Boolean(sources.error) || sources.refreshing}
              aria-labelledby="forward-source-title"
              aria-describedby={
                problem("source") ? "forward-source-error" : undefined
              }
            >
              <ul className="forward-source-list">
                {items.map((source) => (
                  <li key={source.id}>
                    <label>
                      <input
                        type="radio"
                        name="source"
                        checked={!sourceLost && chosen?.id === source.id}
                        onChange={() => setChosen(source)}
                      />
                      <span>
                        <strong>{source.name}</strong>
                        <span>
                          {source.start && source.end
                            ? `行情 ${source.start} 至 ${source.end}`
                            : "行情覆盖未记录"}
                          {" · "}尾号 {source.id.slice(-8)}
                        </span>
                      </span>
                    </label>
                    <details>
                      <summary>完整标识</summary>
                      <code>{source.id}</code>
                    </details>
                  </li>
                ))}
              </ul>
            </fieldset>
          )}
          <div className="forward-create-tools">
            <Button
              onClick={refreshSources}
              loading={sources.refreshing || selection.refreshing}
            >
              {sources.error || (selection.error && !sourceLost)
                ? "重试加载来源"
                : "刷新来源"}
            </Button>
            {sourceOffset > 0 && (
              <Button
                onClick={() =>
                  changeSourcePage(sourceOffset - SOURCE_PAGE_SIZE)
                }
              >
                上一页来源
              </Button>
            )}
            {sources.data?.has_more && (
              <Button
                onClick={() =>
                  changeSourcePage(sourceOffset + SOURCE_PAGE_SIZE)
                }
              >
                下一页来源
              </Button>
            )}
          </div>
        </Section>

        <Section title={<h2 id="forward-pool-title">信号日与固定股票池</h2>}>
          <fieldset
            className="forward-create-fields"
            disabled={locked}
            aria-labelledby="forward-pool-title"
          >
            <Field field="name" label="登记名称" problem={problem("name")}>
              {(props) => (
                <Input
                  {...props}
                  required
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                />
              )}
            </Field>
            <Field
              field="signal"
              label="信号日"
              problem={problem("signal")}
              hint="须在信号日北京时间 00:00 前完成确认。入场日、退出日和日历版本由服务端冻结，登记后可在详情查看。"
            >
              {(props) => (
                <Input
                  {...props}
                  required
                  type="date"
                  value={signalDate}
                  onChange={(event) => setSignalDate(event.target.value)}
                />
              )}
            </Field>
            <div className="forward-create-pool">
              <Field
                field="pool"
                label="固定股票池"
                problem={problem("pool")}
                hint="粘贴 2–50 只 A 股代码，例如 600000.SH、000001.SZ。空格、逗号和换行均可分隔。"
              >
                {(props) => (
                  <Input.TextArea
                    {...props}
                    required
                    rows={3}
                    value={pool}
                    onChange={(event) => setPool(event.target.value)}
                  />
                )}
              </Field>
              <p aria-live="polite">
                {unique.length} 只有效股票
                {duplicateCount > 0 && `，${duplicateCount} 项重复`}
                {invalidCount > 0 && `，${invalidCount} 项格式无效`}
              </p>
              {entries.length > 0 && (
                <ul className="forward-stock-list" aria-label="已输入股票代码">
                  {entries.slice(0, STOCK_LIMIT + 1).map((code, index) => (
                    <li key={index}>
                      <button
                        type="button"
                        aria-label={`移除 ${code}（第 ${index + 1} 项）`}
                        onClick={() => removeStock(index)}
                      >
                        {code} <span aria-hidden="true">×</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              {entries.length > STOCK_LIMIT + 1 && (
                <p>其余代码可在输入框内编辑。</p>
              )}
            </div>
          </fieldset>
        </Section>

        <Section title={<h2 id="forward-momentum-title">动量参数</h2>}>
          <fieldset
            className="forward-create-fields"
            disabled={locked}
            aria-labelledby="forward-momentum-title"
          >
            <Field
              field="window"
              label="动量窗口（交易日）"
              problem={problem("window")}
              hint="2–252，填写整数。"
            >
              {(props) => (
                <Input
                  {...props}
                  required
                  inputMode="numeric"
                  value={window}
                  onChange={(event) => setWindow(event.target.value)}
                />
              )}
            </Field>
            <Field
              field="topK"
              label="前 K 只股票"
              problem={problem("topK")}
              hint="至少 1 只，且少于固定池股票数。"
            >
              {(props) => (
                <Input
                  {...props}
                  required
                  inputMode="numeric"
                  value={topK}
                  onChange={(event) => setTopK(event.target.value)}
                />
              )}
            </Field>
            <Field
              field="direction"
              label="动量方向"
              problem={problem("direction")}
            >
              {(props) => (
                <select
                  {...props}
                  value={direction}
                  onChange={(event) =>
                    setDirection(event.target.value === "low" ? "low" : "high")
                  }
                >
                  <option value="high">高值优先</option>
                  <option value="low">低值优先</option>
                </select>
              )}
            </Field>
          </fieldset>
        </Section>

        <div className="forward-create-submit">
          <Section>
            <p>按动量排序，持有 1 个交易日，比较前 K 等权与登记全池等权。</p>
            <p>
              确认后，本条登记的来源、日历、信号日、股票池和动量参数将冻结。
            </p>
            <div>
              <Button
                type="primary"
                htmlType="submit"
                loading={waiting}
                disabled={locked || !canConfirm}
              >
                {waiting ? "正在确认登记" : "确认登记"}
              </Button>
            </div>
          </Section>
        </div>
      </form>
    </div>
  );
}
