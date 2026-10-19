import { useEffect, useState } from "react";
import { api } from "./api/client";
import { NoticeBanner } from "./components/NoticeBanner";
import { Sidebar, type TabKey } from "./components/Sidebar";
import { useApiResource } from "./hooks/useApiResource";
import { BacktestPage } from "./pages/BacktestPage";
import { DataStatusPage } from "./pages/DataStatusPage";
import { EvaluationPage } from "./pages/EvaluationPage";
import { ExperimentPage } from "./pages/ExperimentPage";
import { FactorRankingPage } from "./pages/FactorRankingPage";
import { StockFactorsPage } from "./pages/StockFactorsPage";
import type { Instrument } from "./types";

const PAGE_META: Record<
  TabKey,
  { eyebrow: string; title: string; subtitle: string }
> = {
  factors: {
    eyebrow: "FACTOR LAB",
    title: "股票因子",
    subtitle:
      "在选定评分批次中查看单只股票的原始因子值、百分位得分与历史变化。",
  },
  ranking: {
    eyebrow: "CROSS-SECTION",
    title: "因子排名",
    subtitle:
      "查看同一股票池中的因子表现，点击股票查看评分详情。",
  },
  backtests: {
    eyebrow: "BACKTEST REVIEW",
    title: "回测结果",
    subtitle:
      "查看已保存回测的净值、回撤、持仓、成交、运行参数与已知限制。",
  },
  experiments: {
    eyebrow: "STRATEGY COMPARISON",
    title: "方案比较",
    subtitle:
      "只读比较历史分段验证的方案结果；测试分段仅复核选定候选，不据此重新选择。",
  },
  data: {
    eyebrow: "数据覆盖",
    title: "数据管理",
    subtitle:
      "只读查看本地数据分区、快照覆盖率与下载任务记录。",
  },
  evaluations: {
    eyebrow: "FACTOR EVALUATION",
    title: "因子评估",
    subtitle:
      "只读查看已保存评估的 IC、分组收益、因子相关性与覆盖情况。",
  },
};

export default function App() {
  const [activeTab, setActiveTab] = useState<TabKey>("factors");
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [selectedInstrument, setSelectedInstrument] =
    useState<Instrument | null>(null);
  const [selectedBacktestId, setSelectedBacktestId] = useState<string | null>(
    null,
  );

  const overview = useApiResource((signal) => api.getOverview(signal), []);
  const runs = useApiResource((signal) => api.getFactorRuns(signal), []);

  useEffect(() => {
    const items = runs.data?.items;
    if (!items?.length) return;
    if (!selectedRunId || !items.some((run) => run.id === selectedRunId)) {
      setSelectedRunId(items[0].id);
    }
  }, [runs.data, selectedRunId]);

  const openStock = (instrument: Instrument) => {
    setSelectedInstrument(instrument);
    setActiveTab("factors");
  };

  const meta = PAGE_META[activeTab];

  return (
    <div className="app-shell">
      <Sidebar
        activeTab={activeTab}
        onTabChange={setActiveTab}
        overview={overview}
      />

      <main className="main">
        <div className="main-inner">
          <header className="page-head">
            <div>
              <div className="eyebrow">{meta.eyebrow}</div>
              <h1>{meta.title}</h1>
              <p className="page-sub">{meta.subtitle}</p>
            </div>
            <div className="page-head-meta">
              <span className="live-dot" aria-hidden="true" />
              <span>本地只读</span>
              <span className="meta-sep" aria-hidden="true">
                ·
              </span>
              <span>研究工作台</span>
            </div>
          </header>

          <NoticeBanner overview={overview} />

          {activeTab === "factors" ? (
            <StockFactorsPage
              runs={runs}
              selectedRunId={selectedRunId}
              onRunChange={setSelectedRunId}
              selectedInstrument={selectedInstrument}
              onInstrumentChange={setSelectedInstrument}
            />
          ) : null}

          {activeTab === "ranking" ? (
            <FactorRankingPage
              runs={runs}
              selectedRunId={selectedRunId}
              onRunChange={setSelectedRunId}
              onOpenStock={openStock}
            />
          ) : null}

          {activeTab === "backtests" ? (
            <BacktestPage
              selectedBacktestId={selectedBacktestId}
              onSelectBacktest={setSelectedBacktestId}
            />
          ) : null}

          {activeTab === "experiments" ? <ExperimentPage /> : null}

          {activeTab === "data" ? <DataStatusPage /> : null}

          {activeTab === "evaluations" ? <EvaluationPage /> : null}

          <footer className="app-footer">
            <span>展示本地已保存的研究结果</span>
            <span className="meta-sep" aria-hidden="true">
              ·
            </span>
            <span>合成样本会在批次与运行中明确标注</span>
          </footer>
        </div>
      </main>
    </div>
  );
}
