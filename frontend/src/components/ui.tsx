import {
  useEffect,
  useRef,
  useState,
  type MouseEvent,
  type ReactNode,
} from "react";
import {
  Alert,
  Button,
  Card,
  Collapse,
  Empty,
  Flex,
  Pagination,
  Result,
  Skeleton,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { ArrowLeftOutlined, InfoCircleOutlined } from "@ant-design/icons";
import { navigate } from "../app/routes";
import { isFiniteNumber } from "../utils/format";

/** An anchor that navigates in-app but still supports open-in-new-tab. */
export function AppLink({
  to,
  children,
  className,
}: {
  to: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <a
      href={to}
      className={className}
      onClick={(event: MouseEvent<HTMLAnchorElement>) => {
        if (
          event.metaKey ||
          event.ctrlKey ||
          event.shiftKey ||
          event.button !== 0
        )
          return;
        event.preventDefault();
        navigate(to);
      }}
    >
      {children}
    </a>
  );
}

export function PageHeader({
  title,
  subtitle,
  extra,
  back,
  tags,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  extra?: ReactNode;
  back?: { to: string; label: string };
  tags?: ReactNode;
}) {
  return (
    <div className="page-header">
      {back ? (
        <AppLink to={back.to} className="page-back">
          <ArrowLeftOutlined /> {back.label}
        </AppLink>
      ) : null}
      <Flex justify="space-between" align="flex-start" gap={16} wrap>
        <div className="page-heading">
          <Flex align="center" gap={8} wrap>
            <Typography.Title level={3} style={{ margin: 0 }}>
              {title}
            </Typography.Title>
            {tags}
          </Flex>
          {subtitle ? (
            <Typography.Text type="secondary" className="page-subtitle">
              {subtitle}
            </Typography.Text>
          ) : null}
        </div>
        {extra ? (
          <Flex gap={8} wrap>
            {extra}
          </Flex>
        ) : null}
      </Flex>
    </div>
  );
}

export function Section({
  title,
  extra,
  children,
  description,
  id,
}: {
  title?: ReactNode;
  extra?: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  id?: string;
}) {
  return (
    <Card
      id={id}
      title={
        title ? (
          <div className="section-title">
            <span>{title}</span>
            {description ? (
              <Typography.Text type="secondary" className="section-description">
                {description}
              </Typography.Text>
            ) : null}
          </div>
        ) : undefined
      }
      extra={extra}
      className="section-card"
    >
      {children}
    </Card>
  );
}

/**
 * A locally scrollable table region: the page keeps the viewport width, the
 * table keeps readable columns, and keyboard users can reach the last column.
 */
export function TableScroll({
  label,
  children,
  hint = true,
}: {
  label: string;
  children: ReactNode;
  hint?: boolean;
}) {
  const region = useRef<HTMLDivElement | null>(null);
  const [scrollable, setScrollable] = useState(false);

  useEffect(() => {
    const node = region.current;
    if (!node) return;
    const update = () => setScrollable(node.scrollWidth > node.clientWidth + 1);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(node);
    // A page change can keep the wrapper's height while the inner table gets
    // wider or narrower, so watch the table itself and any DOM replacement.
    const inner = node.querySelector("table");
    if (inner) observer.observe(inner);
    const mutations =
      typeof MutationObserver === "undefined"
        ? null
        : new MutationObserver(update);
    mutations?.observe(node, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["style", "class"],
    });
    return () => {
      observer.disconnect();
      mutations?.disconnect();
    };
  }, [children]);

  return (
    <div className="table-scroll-host">
      <div
        ref={region}
        className="table-scroll"
        tabIndex={0}
        role="region"
        aria-label={label}
      >
        {children}
      </div>
      {hint && scrollable ? (
        <Typography.Text type="secondary" className="table-scroll-hint">
          左右滑动或聚焦后用方向键查看更多列
        </Typography.Text>
      ) : null}
    </div>
  );
}

export function Loading({ rows = 4 }: { rows?: number }) {
  return <Skeleton active paragraph={{ rows }} />;
}

export function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <Result
      status="warning"
      title="加载失败"
      subTitle={message}
      extra={onRetry ? <Button onClick={onRetry}>重试</Button> : undefined}
    />
  );
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <Empty
      image={Empty.PRESENTED_IMAGE_SIMPLE}
      description={
        <Flex vertical gap={4} align="center">
          <Typography.Text strong>{title}</Typography.Text>
          {description ? (
            <Typography.Text type="secondary">{description}</Typography.Text>
          ) : null}
        </Flex>
      }
    >
      {action}
    </Empty>
  );
}

/** Render loading / error / empty states for an API resource consistently. */
export function Resource<T>({
  state,
  empty,
  children,
  rows,
}: {
  state: {
    data: T | null;
    loading: boolean;
    error: string | null;
    reload: () => void;
  };
  empty?: (data: T) => ReactNode | null;
  children: (data: T) => ReactNode;
  rows?: number;
}) {
  if (state.data === null) {
    if (state.error)
      return <ErrorState message={state.error} onRetry={state.reload} />;
    return <Loading rows={rows} />;
  }
  const emptyView = empty?.(state.data);
  if (emptyView) return <>{emptyView}</>;
  return (
    <>
      {state.error ? (
        <Alert
          type="warning"
          showIcon
          title={`刷新失败：${state.error}`}
          style={{ marginBottom: 12 }}
        />
      ) : null}
      {children(state.data)}
    </>
  );
}

const STATUS: Record<string, { color: string; label: string }> = {
  queued: { color: "default", label: "排队中" },
  running: { color: "processing", label: "运行中" },
  succeeded: { color: "success", label: "已完成" },
  complete: { color: "success", label: "已完成" },
  failed: { color: "error", label: "失败" },
  cancelled: { color: "default", label: "已取消" },
  interrupted: { color: "warning", label: "已中断" },
  paused: { color: "warning", label: "已暂停" },
  partial: { color: "warning", label: "部分完成" },
  not_started: { color: "default", label: "未开始" },
  incomplete: { color: "warning", label: "不完整" },
};

export function StatusTag({
  status,
  label,
}: {
  status: string;
  label?: string;
}) {
  const meta = STATUS[status] ?? { color: "default", label: status };
  return (
    <Tag color={meta.color} variant="filled">
      {label ?? meta.label}
    </Tag>
  );
}

/** A signed value coloured by the user's up/down convention. */
export function Signed({
  value,
  format,
  neutral = false,
}: {
  value: number | null | undefined;
  format: (value: number) => string;
  neutral?: boolean;
}) {
  if (!isFiniteNumber(value)) return <span className="muted">—</span>;
  const className =
    neutral || value === 0 ? "" : value > 0 ? "quant-up" : "quant-down";
  return <span className={`numeric ${className}`}>{format(value)}</span>;
}

export function MonoId({
  value,
  copy = true,
}: {
  value: string | null | undefined;
  copy?: boolean;
}) {
  if (!value) return <span className="muted">—</span>;
  return (
    <Typography.Text
      code
      copyable={copy ? { text: value } : false}
      className="mono-id"
    >
      {value}
    </Typography.Text>
  );
}

/** Methodology and limitation text, collapsed by default instead of filling the page. */
export function Notes({
  title = "口径与限制",
  items,
  defaultOpen = false,
}: {
  title?: string;
  items: Array<ReactNode | null | undefined | false>;
  defaultOpen?: boolean;
}) {
  const visible = items.filter(Boolean);
  if (!visible.length) return null;
  return (
    <Collapse
      size="small"
      ghost
      defaultActiveKey={defaultOpen ? ["notes"] : []}
      className="notes"
      items={[
        {
          key: "notes",
          label: (
            <Typography.Text type="secondary">
              <InfoCircleOutlined /> {title}
            </Typography.Text>
          ),
          children: (
            <ul className="notes-list">
              {visible.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          ),
        },
      ]}
    />
  );
}

export function Hint({ text }: { text: ReactNode }) {
  return (
    <Tooltip title={text}>
      <InfoCircleOutlined className="hint-icon" />
    </Tooltip>
  );
}

export function WrapTag({
  text,
  maxChars = 40,
}: {
  text: string;
  maxChars?: number;
}) {
  const clamped = Array.from(text).length > maxChars;
  return (
    <Tag title={clamped ? text : undefined}>
      {clamped ? <span className="tag-text-clamp">{text}</span> : text}
    </Tag>
  );
}

export function CollapsibleTagList({
  tags,
  maxTagChars = 14,
}: {
  tags: Array<{ key: string; label: string }>;
  maxTagChars?: number;
}) {
  const [expanded, setExpanded] = useState(false);
  if (!tags.length) return null;
  const anyLong = tags.some((t) => Array.from(t.label).length > maxTagChars);
  const collapsedCount = anyLong ? 1 : Math.min(2, tags.length);
  const visible = expanded ? tags : tags.slice(0, collapsedCount);
  const hiddenCount = Math.max(0, tags.length - collapsedCount);
  const needsToggle = hiddenCount > 0 || anyLong;
  return (
    <div className="tag-list-row">
      <div
        className={
          expanded ? "tag-list-inline tag-list-inline-open" : "tag-list-inline"
        }
      >
        {visible.map((t) => (
          <Tag
            key={t.key}
            title={t.label}
            className={expanded ? "tag-expanded-item" : "tag-single-line"}
          >
            {expanded ? (
              t.label
            ) : (
              <span className="tag-text-single">{t.label}</span>
            )}
          </Tag>
        ))}
      </div>
      {needsToggle ? (
        <button
          type="button"
          className="task-reason-toggle tag-expand-toggle"
          aria-expanded={expanded}
          onClick={() => setExpanded((prev) => !prev)}
        >
          {expanded
            ? "收起因子"
            : hiddenCount > 0
              ? `+${hiddenCount} 个因子`
              : "展开因子"}
        </button>
      ) : null}
    </div>
  );
}

export interface MobileCardItem {
  key: string;
  title: string;
  badges?: ReactNode;
  subBadges?: ReactNode;
  meta?: Array<ReactNode | null | undefined | false>;
  tags?: ReactNode;
  onClick?: () => void;
}

const MOBILE_PAGE_SIZE = 20;

export function MobileCardList({
  items,
  paginate = true,
  resetKey,
}: {
  items: MobileCardItem[];
  paginate?: boolean;
  resetKey?: string;
}) {
  const [page, setPage] = useState(1);
  const maxPage = Math.max(1, Math.ceil(items.length / MOBILE_PAGE_SIZE));
  useEffect(() => {
    setPage(1);
  }, [resetKey]);
  useEffect(() => {
    if (page > maxPage) setPage(maxPage);
  }, [page, maxPage]);
  const activePage = Math.min(page, maxPage);
  const visible = paginate
    ? items.slice(
        (activePage - 1) * MOBILE_PAGE_SIZE,
        activePage * MOBILE_PAGE_SIZE,
      )
    : items;

  return (
    <div className="list-cards-wrap">
      <div className="list-cards" role="list">
        {visible.map((item) => {
          const metaItems = (item.meta ?? []).filter(Boolean);
          const content = (
            <>
              <div className="list-card-head">
                <div className="list-card-title">
                  <span className="list-card-title-clamp" title={item.title}>
                    {item.title}
                  </span>
                </div>
                {item.badges ? (
                  <div className="list-card-badges">{item.badges}</div>
                ) : null}
              </div>
              {item.subBadges ? (
                <div className="list-card-subbadges">{item.subBadges}</div>
              ) : null}
              {metaItems.length ? (
                <div className="list-card-meta">
                  {metaItems.map((entry, idx) => (
                    <span key={idx} className="list-card-meta-item">
                      {entry}
                    </span>
                  ))}
                </div>
              ) : null}
            </>
          );
          const cardClass = item.tags
            ? "list-card list-card-has-tags"
            : "list-card";
          return (
            <div key={item.key} role="listitem" className={cardClass}>
              {item.onClick ? (
                <button
                  type="button"
                  className="list-card-trigger"
                  onClick={item.onClick}
                >
                  {content}
                </button>
              ) : (
                content
              )}
              {item.tags ? (
                <div className="list-card-tags">{item.tags}</div>
              ) : null}
            </div>
          );
        })}
      </div>
      {paginate && items.length > MOBILE_PAGE_SIZE ? (
        <Flex justify="end" style={{ marginTop: 12 }}>
          <Pagination
            current={activePage}
            pageSize={MOBILE_PAGE_SIZE}
            total={items.length}
            onChange={setPage}
            showSizeChanger={false}
          />
        </Flex>
      ) : null}
    </div>
  );
}
