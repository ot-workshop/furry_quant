import { Tabs } from "antd";
import { lazy, Suspense } from "react";
import { setSearch } from "../app/routes";
import { Loading, PageHeader } from "../components/ui";
const Datasets = lazy(() => import("./data/Datasets"));
const MarketDownload = lazy(() => import("./data/MarketDownload"));
const FinancialData = lazy(() => import("./data/FinancialData"));
const IndustryData = lazy(() => import("./data/IndustryData"));
const Database = lazy(() => import("./data/Database"));
const UnifiedUpdates = lazy(() => import("./data/UnifiedUpdates"));
const Backups = lazy(() => import("./data/Backups"));
export default function DataPage({ tab }: { tab: string | null }) {
  return (
    <>
      <PageHeader
        title="数据管理"
        subtitle="下载并核验行情，生成可用于研究的数据集"
      />
      <Suspense fallback={<Loading />}>
        <Tabs
          activeKey={tab ?? "datasets"}
          onChange={(value) =>
            setSearch({ tab: value === "datasets" ? null : value })
          }
          items={[
            { key: "updates", label: "统一更新", children: <UnifiedUpdates /> },
            { key: "backups", label: "备份与恢复", children: <Backups /> },
            { key: "datasets", label: "研究数据集", children: <Datasets /> },
            { key: "market", label: "行情下载", children: <MarketDownload /> },
            {
              key: "financial",
              label: "财务数据",
              children: <FinancialData />,
            },
            { key: "industry", label: "行业数据", children: <IndustryData /> },
            { key: "database", label: "数据库", children: <Database /> },
          ]}
        />
      </Suspense>
    </>
  );
}
