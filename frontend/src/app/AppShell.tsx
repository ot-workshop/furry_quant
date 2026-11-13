import { useState, type ReactNode } from "react";
import {
  Badge,
  Button,
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
  FundOutlined,
  LineChartOutlined,
  MoonOutlined,
  PlusCircleOutlined,
  SunOutlined,
  UnorderedListOutlined,
} from "@ant-design/icons";
import { researchApi } from "../api/research";
import { useApiResource } from "../hooks/useApiResource";
import { navigate, paths, useRoute, type Section } from "./routes";
import { useTheme, type ThemeMode } from "./theme";

const MENU: MenuProps["items"] = [
  { key: "dashboard", icon: <DashboardOutlined />, label: "工作台" },
  {
    type: "group",
    label: "研究",
    children: [
      { key: "research", icon: <PlusCircleOutlined />, label: "新建研究" },
      { key: "experiments", icon: <ApartmentOutlined />, label: "方案比较" },
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
  tasks: paths.tasks(),
  backtests: paths.backtests(),
  factors: paths.factors(),
  evaluations: paths.evaluations(),
  experiments: paths.experiments(),
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
  const tasks = useApiResource((signal) => researchApi.tasks(signal), [], {
    pollMs: 5000,
  });
  const active = (tasks.data?.items ?? []).filter(
    (t) => t.status === "queued" || t.status === "running",
  ).length;

  return (
    <Layout className="app-layout">
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
      <Layout>
        <Layout.Header className="app-header">
          <Flex
            justify="flex-end"
            align="center"
            gap={8}
            style={{ height: "100%" }}
          >
            <Tooltip
              title={
                active ? `${active} 个任务排队或运行中` : "没有进行中的任务"
              }
            >
              <Badge count={active} size="small" offset={[-4, 4]}>
                <Button
                  type="text"
                  icon={<UnorderedListOutlined />}
                  onClick={() => navigate(paths.tasks())}
                >
                  任务
                </Button>
              </Badge>
            </Tooltip>
            <Button
              type="primary"
              icon={<PlusCircleOutlined />}
              onClick={() => navigate(paths.newResearch())}
            >
              新建研究
            </Button>
            <ThemeSwitch />
          </Flex>
        </Layout.Header>
        <Layout.Content className="app-content">
          <div className="app-content-inner">{children}</div>
        </Layout.Content>
      </Layout>
    </Layout>
  );
}
