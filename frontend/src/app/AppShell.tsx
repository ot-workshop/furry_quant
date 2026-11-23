import { useState, type ReactNode } from "react";
import {
  Badge,
  Button,
  Drawer,
  Dropdown,
  Flex,
  Layout,
  Menu,
  Segmented,
  Tooltip,
  Typography,
} from "antd";
import type { MenuProps } from "antd";
import {
  ApartmentOutlined,
  BarChartOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  DiffOutlined,
  FileSearchOutlined,
  FundOutlined,
  LineChartOutlined,
  MenuOutlined,
  MoonOutlined,
  PlusCircleOutlined,
  SunOutlined,
  UnorderedListOutlined,
} from "@ant-design/icons";
import { researchApi } from "../api/research";
import { useApiResource } from "../hooks/useApiResource";
import { navigate, paths, useRoute, type Section } from "./routes";
import { useTheme, type ThemeMode } from "./theme";
import { useMediaQuery } from "./useMediaQuery";

/** Below this width the sidebar becomes a drawer so result pages keep the full width. */
const COMPACT_QUERY = "(max-width: 767px)";
/** Below this width the header keeps icon-only actions so they never crowd. */
const NARROW_QUERY = "(max-width: 479px)";

const MENU: MenuProps["items"] = [
  { key: "dashboard", icon: <DashboardOutlined />, label: "工作台" },
  {
    type: "group",
    label: "研究",
    children: [
      { key: "research", icon: <PlusCircleOutlined />, label: "新建研究" },
      { key: "experiments", icon: <ApartmentOutlined />, label: "方案比较" },
      { key: "compare", icon: <DiffOutlined />, label: "差异对照" },
      {
        key: "forward-registrations",
        icon: <FileSearchOutlined />,
        label: "前瞻登记",
      },
      { key: "tasks", icon: <UnorderedListOutlined />, label: "任务中心" },
    ],
  },
  {
    type: "group",
    label: "结果",
    children: [
      { key: "backtests", icon: <LineChartOutlined />, label: "回测" },
      { key: "factors", icon: <BarChartOutlined />, label: "因子评分" },
      { key: "evaluations", icon: <FundOutlined />, label: "因子评估" },
    ],
  },
  {
    type: "group",
    label: "数据",
    children: [{ key: "data", icon: <DatabaseOutlined />, label: "数据管理" }],
  },
];

const TARGETS: Record<Section, string> = {
  dashboard: paths.dashboard(),
  research: paths.newResearch(),
  "forward-registrations": paths.forwardRegistrations(),
  tasks: paths.tasks(),
  backtests: paths.backtests(),
  factors: paths.factors(),
  evaluations: paths.evaluations(),
  experiments: paths.experiments(),
  compare: paths.compare(),
  data: paths.data(),
};

function ThemeSwitch() {
  const { mode, setMode, name, redUp, setRedUp } = useTheme();
  return (
    <Dropdown
      trigger={["click"]}
      popupRender={() => (
        <div className="settings-popover">
          <Typography.Text type="secondary">界面主题</Typography.Text>
          <Segmented<ThemeMode>
            block
            value={mode}
            onChange={setMode}
            options={[
              { value: "light", label: "浅色" },
              { value: "dark", label: "深色" },
              { value: "system", label: "跟随系统" },
            ]}
          />
          <Typography.Text type="secondary">涨跌颜色</Typography.Text>
          <Segmented
            block
            value={redUp ? "red" : "green"}
            onChange={(value) => setRedUp(value === "red")}
            options={[
              { value: "red", label: "红涨绿跌" },
              { value: "green", label: "绿涨红跌" },
            ]}
          />
        </div>
      )}
    >
      <Tooltip title="主题与涨跌颜色">
        <Button
          type="text"
          icon={name === "dark" ? <MoonOutlined /> : <SunOutlined />}
          aria-label="显示设置"
        />
      </Tooltip>
    </Dropdown>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const route = useRoute();
  const [collapsed, setCollapsed] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const compact = useMediaQuery(COMPACT_QUERY);
  const narrow = useMediaQuery(NARROW_QUERY);
  const tasks = useApiResource((signal) => researchApi.tasks(signal), [], {
    pollMs: 5000,
  });
  const active = (tasks.data?.items ?? []).filter(
    (t) => t.status === "queued" || t.status === "running",
  ).length;
  const go = (key: string) => {
    setNavOpen(false);
    navigate(TARGETS[key as Section]);
  };

  return (
    <Layout className="app-layout">
      {compact ? null : (
        <Layout.Sider
          collapsible
          collapsed={collapsed}
          onCollapse={setCollapsed}
          width={216}
          theme="light"
          className="app-sider"
          breakpoint="lg"
        >
          <div
            className="brand"
            onClick={() => navigate(paths.dashboard())}
            role="link"
            tabIndex={0}
          >
            <span className="brand-mark">Q</span>
            {!collapsed ? <span className="brand-name">量化研究台</span> : null}
          </div>
          <Menu
            className="app-menu"
            mode="inline"
            selectedKeys={[route.section]}
            items={MENU}
            onClick={({ key }) => navigate(TARGETS[key as Section])}
          />
        </Layout.Sider>
      )}
      {compact ? (
        <Drawer
          open={navOpen}
          onClose={() => setNavOpen(false)}
          placement="left"
          size="min(280px, 85vw)"
          title="量化研究台"
          className="app-nav-drawer"
          styles={{ body: { padding: 8 } }}
        >
          <Menu
            mode="inline"
            selectedKeys={[route.section]}
            items={MENU}
            style={{ borderInlineEnd: 0 }}
            onClick={({ key }) => go(key)}
          />
        </Drawer>
      ) : null}
      <Layout>
        <Layout.Header className="app-header">
          <Flex
            justify="space-between"
            align="center"
            gap={8}
            style={{ height: "100%" }}
          >
            <Flex align="center" gap={8} style={{ minWidth: 0 }}>
              {compact ? (
                <Button
                  type="text"
                  icon={<MenuOutlined />}
                  aria-label="打开导航"
                  data-testid="nav-toggle"
                  onClick={() => setNavOpen(true)}
                />
              ) : null}
            </Flex>
            <Flex align="center" gap={8} className="app-header-actions">
              <Tooltip
                title={
                  active ? `${active} 个任务排队或运行中` : "没有进行中的任务"
                }
              >
                <Badge count={active} size="small" offset={[-4, 4]}>
                  <Button
                    type="text"
                    icon={<UnorderedListOutlined />}
                    aria-label={narrow ? "任务" : undefined}
                    onClick={() => navigate(paths.tasks())}
                  >
                    {narrow ? null : "任务"}
                  </Button>
                </Badge>
              </Tooltip>
              <Button
                type="primary"
                icon={<PlusCircleOutlined />}
                aria-label={narrow ? "新建研究" : undefined}
                onClick={() => navigate(paths.newResearch())}
              >
                {narrow ? null : "新建研究"}
              </Button>
              <ThemeSwitch />
            </Flex>
          </Flex>
        </Layout.Header>
        <Layout.Content className="app-content">
          <div className="app-content-inner">{children}</div>
        </Layout.Content>
      </Layout>
    </Layout>
  );
}
