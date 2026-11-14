import { lazy, Suspense } from "react";
import { AppShell } from "./app/AppShell";
import { useRoute, type Route } from "./app/routes";
import { Loading } from "./components/ui";

const DashboardPage = lazy(() => import("./pages/DashboardPage"));
const ResearchNewPage = lazy(() => import("./pages/ResearchNewPage"));
const TasksPage = lazy(() => import("./pages/TasksPage"));
const BacktestListPage = lazy(() => import("./pages/BacktestListPage"));
const BacktestDetailPage = lazy(() => import("./pages/BacktestDetailPage"));
const FactorsPage = lazy(() => import("./pages/FactorsPage"));
const EvaluationListPage = lazy(() => import("./pages/EvaluationListPage"));
const EvaluationDetailPage = lazy(() => import("./pages/EvaluationDetailPage"));
const ExperimentListPage = lazy(() => import("./pages/ExperimentListPage"));
const ExperimentDetailPage = lazy(() => import("./pages/ExperimentDetailPage"));
const ExperimentNewPage = lazy(() => import("./pages/ExperimentNewPage"));
const DataPage = lazy(() => import("./pages/DataPage"));

function Page({ route }: { route: Route }) {
  const [first, second] = route.params;
  switch (route.section) {
    case "research":
      return (
        <ResearchNewPage
          fromTask={route.search.get("from")}
          marketVersion={route.search.get("market")}
        />
      );
    case "tasks":
      return <TasksPage focus={route.search.get("focus")} />;
    case "backtests":
      return first ? (
        <BacktestDetailPage id={first} tab={second ?? "overview"} />
      ) : (
        <BacktestListPage />
      );
    case "factors":
      return <FactorsPage search={route.search} />;
    case "evaluations":
      return first ? (
        <EvaluationDetailPage id={first} />
      ) : (
        <EvaluationListPage />
      );
    case "experiments":
      if (first === "new") return <ExperimentNewPage />;
      return first ? (
        <ExperimentDetailPage id={first} />
      ) : (
        <ExperimentListPage />
      );
    case "data":
      return <DataPage tab={route.search.get("tab")} />;
    default:
      return <DashboardPage />;
  }
}

export default function App() {
  const route = useRoute();
  return (
    <AppShell>
      <Suspense fallback={<Loading rows={8} />}>
        <Page route={route} />
      </Suspense>
    </AppShell>
  );
}
