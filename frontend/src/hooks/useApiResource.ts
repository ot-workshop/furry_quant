import { useCallback, useEffect, useRef, useState } from "react";
import { describeError, isAbortError } from "../api/client";

export interface ApiState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
}

interface Snapshot<T> {
  key: string;
  data: T | null;
  loading: boolean;
  error: string | null;
}

/**
 * 统一的只读请求状态。
 * - 依赖变化时中止上一个请求，旧响应即使晚到也不会写入当前状态。
 * - 依赖变化后的首个渲染即返回空数据，避免旧选择的数据短暂混入新选择。
 * - load 传 null 表示当前缺少必要参数，不发起请求。
 *
 * deps 只应包含原始值（字符串、数字、布尔值等）。
 */
export function useApiResource<T>(
  load: ((signal: AbortSignal) => Promise<T>) | null,
  deps: readonly unknown[],
): ApiState<T> {
  const [version, setVersion] = useState(0);
  const [snapshot, setSnapshot] = useState<Snapshot<T>>({
    key: "",
    data: null,
    loading: Boolean(load),
    error: null,
  });
  const loadRef = useRef(load);

  loadRef.current = load;

  const reload = useCallback(() => {
    setVersion((current) => current + 1);
  }, []);

  const requestKey = `${version}:${JSON.stringify(deps)}`;

  useEffect(() => {
    const currentLoad = loadRef.current;

    if (!currentLoad) {
      setSnapshot({
        key: requestKey,
        data: null,
        loading: false,
        error: null,
      });
      return;
    }

    const controller = new AbortController();
    let active = true;

    setSnapshot({
      key: requestKey,
      data: null,
      loading: true,
      error: null,
    });

    currentLoad(controller.signal)
      .then((result) => {
        if (!active) return;
        setSnapshot({
          key: requestKey,
          data: result,
          loading: false,
          error: null,
        });
      })
      .catch((requestError: unknown) => {
        if (!active || isAbortError(requestError)) return;
        setSnapshot({
          key: requestKey,
          data: null,
          loading: false,
          error: describeError(requestError),
        });
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [requestKey]);

  const isCurrent = snapshot.key === requestKey;

  return {
    data: isCurrent ? snapshot.data : null,
    loading: isCurrent ? snapshot.loading : Boolean(load),
    error: isCurrent ? snapshot.error : null,
    reload,
  };
}
