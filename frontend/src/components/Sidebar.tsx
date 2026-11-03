import type { ReactNode } from "react";
import type { ApiState } from "../hooks/useApiResource";
import type { Overview } from "../types";
import { overviewModeLabel } from "../utils/format";

export type TabKey =
  | "research"
  | "factors"
  | "ranking"
  | "backtests"
  | "experiments"
  | "data"
  | "evaluations";

interface NavItem {
  key: TabKey;
  label: string;
  caption: string;
  icon: ReactNode;
}

const NAV_ITEMS: NavItem[] = [
  {
    key: "research",
    label: "新建研究",
    caption: "配置与任务运行",
    icon: (
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" aria-hidden="true">
        <path
          d="M12 5v14M5 12h14"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
        />
        <circle
          cx="12"
          cy="12"
          r="8"
          stroke="currentColor"
          strokeWidth="1.4"
          opacity="0.55"
        />
      </svg>
    ),
  },
  {
    key: "factors",
    label: "股票因子",
    caption: "个股评分与历史",
    icon: (
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" aria-hidden="true">
        <path
          d="M5 19V10M12 19V5M19 19v-6"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
        />
      </svg>
    ),
  },
  {
    key: "ranking",
    label: "因子排名",
    caption: "横截面比较",
    icon: (
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" aria-hidden="true">
        <path
          d="M5 7h14M5 12h10M5 17h7"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
        />
      </svg>
    ),
  },
  {
    key: "backtests",
    label: "回测结果",
    caption: "净值、持仓与成交",
    icon: (
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" aria-hidden="true">
        <path
          d="M4 16.5 9 11l3.5 3L20 6.5"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <path d="M15.5 6.5H20V11" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      </svg>
    ),
  },
  {
    key: "experiments",
    label: "方案比较",
    caption: "历史分段验证",
    icon: (
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" aria-hidden="true">
        <rect
          x="4"
          y="5"
          width="7"
          height="6"
          rx="1.2"
          stroke="currentColor"
          strokeWidth="1.8"
        />
        <rect
          x="13"
          y="13"
          width="7"
          height="6"
          rx="1.2"
          stroke="currentColor"
          strokeWidth="1.8"
        />
        <path
          d="M14 8h4.5M16.5 5.5 19 8l-2.5 2.5M10 16H5.5M7.5 13.5 5 16l2.5 2.5"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    ),
  },
  {
    key: "data",
    label: "数据管理",
    caption: "覆盖情况与下载记录",
    icon: (
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" aria-hidden="true">
        <ellipse
          cx="12"
          cy="5.5"
          rx="7"
          ry="2.8"
          stroke="currentColor"
          strokeWidth="1.8"
        />
        <path
          d="M5 5.5v6c0 1.55 3.13 2.8 7 2.8s7-1.25 7-2.8v-6"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
        />
        <path
          d="M5 11.5v6c0 1.55 3.13 2.8 7 2.8s7-1.25 7-2.8v-6"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
        />
      </svg>
    ),
  },
  {
    key: "evaluations",
    label: "因子评估",
    caption: "IC 与分组诊断",
    icon: (
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" aria-hidden="true">
        <path
          d="M4 18h16M7 15V9M12 15V5M17 15v-4"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
        />
        <path
          d="m5 6 3-2 3 2 4-3 4 3"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    ),
  },
];

interface SidebarProps {
  activeTab: TabKey;
  onTabChange: (tab: TabKey) => void;
  overview: ApiState<Overview>;
}

export function Sidebar({ activeTab, onTabChange, overview }: SidebarProps) {
  const data = overview.data;
  const modeLabel = overview.loading
    ? "读取模式"
    : overview.error
      ? "模式未知"
      : overviewModeLabel(data?.mode);

  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark" aria-hidden="true">
          Q
        </div>
        <div>
          <div className="brand-title">量化研究台</div>
          <div className="brand-sub">LOCAL RESEARCH CONSOLE</div>
        </div>
      </div>

      <nav className="nav" aria-label="主导航">
        {NAV_ITEMS.map((item) => {
          const active = item.key === activeTab;
          return (
            <button
              key={item.key}
              type="button"
              className={`nav-button${active ? " active" : ""}`}
              aria-current={active ? "page" : undefined}
              onClick={() => onTabChange(item.key)}
            >
              <span className="nav-icon">{item.icon}</span>
              <span className="nav-copy">
                <span className="nav-label">{item.label}</span>
                <span className="nav-caption">{item.caption}</span>
              </span>
            </button>
          );
        })}
      </nav>

      <div className="sidebar-bottom">
        <div>
          <div className="sidebar-section-title">数据规模</div>
          <div className="sidebar-counts">
            <div>
              <span>因子批次</span>
              <strong>{data ? data.factor_runs : "—"}</strong>
            </div>
            <div>
              <span>回测</span>
              <strong>{data ? data.backtests : "—"}</strong>
            </div>
            <div>
              <span>标的</span>
              <strong>{data ? data.instruments : "—"}</strong>
            </div>
          </div>
        </div>
        <div className={`sidebar-mode${overview.error ? " error" : ""}`}>
          <span className="sidebar-mode-dot" aria-hidden="true" />
          <span>{modeLabel}</span>
        </div>
        <p className="sidebar-note">
          本地研究工作台 · 查看评分与已完成的回测。
        </p>
      </div>
    </aside>
  );
}
