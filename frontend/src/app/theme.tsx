import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { App as AntApp, ConfigProvider, theme as antTheme } from "antd";
import zhCN from "antd/locale/zh_CN";

export type ThemeMode = "light" | "dark" | "system";
export type ThemeName = "light" | "dark";

/**
 * The only colour definitions in the application. AntD components, custom CSS
 * (through AntD CSS variables) and ECharts all derive from these values, so
 * light and dark modes stay consistent everywhere.
 */
const BRAND = "#2f6fed";
const SERIES = {
  light: ["#2f6fed", "#8c8c8c", "#7c4dff", "#fa8c16", "#13a8a8", "#eb2f96"],
  dark: ["#5b8cff", "#a6a6a6", "#a58bff", "#ffa940", "#36cfc9", "#f759ab"],
};
const MARKET = {
  light: { up: "#cf1322", down: "#389e0d" },
  dark: { up: "#ff4d4f", down: "#52c41a" },
};

const STORAGE_MODE = "quant-theme";
const STORAGE_RED_UP = "quant-red-up";

function readPreference(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writePreference(key: string, value: string) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* preferences stay in memory when storage is unavailable */
  }
}

interface ThemeState {
  name: ThemeName;
  mode: ThemeMode;
  redUp: boolean;
  setMode: (mode: ThemeMode) => void;
  setRedUp: (value: boolean) => void;
}

const ThemeContext = createContext<ThemeState>({
  name: "light",
  mode: "system",
  redUp: true,
  setMode: () => undefined,
  setRedUp: () => undefined,
});

export const useTheme = () => useContext(ThemeContext);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<ThemeMode>(() => {
    const saved = readPreference(STORAGE_MODE);
    return saved === "light" || saved === "dark" ? saved : "system";
  });
  const [redUp, setRedUp] = useState(
    () => readPreference(STORAGE_RED_UP) !== "false",
  );
  const [systemDark, setSystemDark] = useState(
    () => window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false,
  );
  const name: ThemeName =
    mode === "system" ? (systemDark ? "dark" : "light") : mode;

  useEffect(() => {
    const query = window.matchMedia?.("(prefers-color-scheme: dark)");
    const change = () => setSystemDark(query?.matches ?? false);
    query?.addEventListener("change", change);
    return () => query?.removeEventListener("change", change);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = name;
    document.documentElement.style.colorScheme = name;
    writePreference(STORAGE_MODE, mode);
    writePreference(STORAGE_RED_UP, String(redUp));
  }, [mode, name, redUp]);

  const value = useMemo(
    () => ({ name, mode, redUp, setMode, setRedUp }),
    [name, mode, redUp],
  );
  const market = MARKET[name];

  return (
    <ThemeContext.Provider value={value}>
      <ConfigProvider
        locale={zhCN}
        theme={{
          cssVar: { key: "quant" },
          algorithm:
            name === "dark"
              ? antTheme.darkAlgorithm
              : antTheme.defaultAlgorithm,
          token: {
            colorPrimary: BRAND,
            colorInfo: BRAND,
            borderRadius: 8,
            fontSize: 14,
            fontFamily:
              '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
          },
          components: {
            Layout: {
              headerHeight: 56,
              headerPadding: "0 24px",
              ...(name === "light"
                ? {
                    bodyBg: "#f5f7fa",
                    headerBg: "#ffffff",
                    siderBg: "#ffffff",
                    triggerBg: "#ffffff",
                    triggerColor: "rgba(0,0,0,0.65)",
                  }
                : {
                    bodyBg: "#0a0a0a",
                    headerBg: "#141414",
                    siderBg: "#141414",
                    triggerBg: "#141414",
                  }),
            },
            Menu: { itemBorderRadius: 8, activeBarBorderWidth: 0 },
            Table: { headerBorderRadius: 8 },
          },
        }}
      >
        <AntApp>
          <div
            className="theme-root"
            style={
              {
                "--quant-up": redUp ? market.up : market.down,
                "--quant-down": redUp ? market.down : market.up,
              } as React.CSSProperties
            }
          >
            {children}
          </div>
        </AntApp>
      </ConfigProvider>
    </ThemeContext.Provider>
  );
}

export interface ChartPalette {
  text: string;
  muted: string;
  border: string;
  split: string;
  surface: string;
  primary: string;
  primarySoft: string;
  benchmark: string;
  accent: string;
  up: string;
  down: string;
  series: string[];
}

/** Colours for canvas charts, which cannot read CSS variables. */
export function useChartPalette(): ChartPalette {
  const { name, redUp } = useTheme();
  const { token } = antTheme.useToken();
  return useMemo(() => {
    const market = MARKET[name];
    const series = SERIES[name];
    return {
      text: token.colorText,
      muted: token.colorTextSecondary,
      border: token.colorBorder,
      split: token.colorBorderSecondary,
      surface: token.colorBgElevated,
      primary: series[0],
      primarySoft:
        name === "dark" ? "rgba(91,140,255,0.16)" : "rgba(47,111,237,0.10)",
      benchmark: series[1],
      accent: series[2],
      up: redUp ? market.up : market.down,
      down: redUp ? market.down : market.up,
      series,
    };
  }, [name, redUp, token]);
}
