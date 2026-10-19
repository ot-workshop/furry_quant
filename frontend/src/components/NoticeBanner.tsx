import type { ApiState } from "../hooks/useApiResource";
import type { Overview } from "../types";
import { overviewModeLabel } from "../utils/format";

interface NoticeBannerProps {
  overview: ApiState<Overview>;
}

export function NoticeBanner({ overview }: NoticeBannerProps) {
  const data = overview.data;
  const mode = data?.mode?.trim().toLowerCase();
  const synthetic = mode === "synthetic";

  return (
    <div
      className={`notice-bar${overview.error ? " notice-error" : ""}`}
      role="status"
      aria-live="polite"
    >
      <span className={`notice-tag${synthetic ? "" : " notice-tag-neutral"}`}>
        {overview.loading
          ? "数据模式"
          : overview.error
            ? "模式未知"
            : overviewModeLabel(data?.mode)}
      </span>
      <div className="notice-copy">
        {overview.loading ? (
          "正在读取数据模式与说明…"
        ) : overview.error ? (
          <>
            无法读取数据说明：{overview.error}。
          </>
        ) : (
          <>
            {data?.notice || "暂无数据说明。"}
            {synthetic ? (
              <span className="notice-foot">合成样本环境 · 结果展示</span>
            ) : null}
          </>
        )}
      </div>
    </div>
  );
}
