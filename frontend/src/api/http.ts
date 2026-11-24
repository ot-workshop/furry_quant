export const API_BASE = "/api/v1";

export class ApiError extends Error {
  readonly status: number;

  constructor(
    message: string,
    status: number,
    readonly code?: string,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    (error as { name?: unknown }).name === "AbortError"
  );
}

export function describeError(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "发生未知错误";
}

type QueryValue = string | number | boolean | null | undefined;

export function query(params: Record<string, QueryValue>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
}

/** The single HTTP entry point; server error details are surfaced verbatim. */
export async function requestWithStatus<T>(
  path: string,
  { method = "GET", body, headers, signal }: RequestOptions = {},
): Promise<{ status: number; data: T }> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      signal,
      headers: {
        Accept: "application/json",
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...headers,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw new ApiError("无法连接后端服务，请确认 API 已启动", 0);
  }
  if (!response.ok) {
    let detail = `请求失败（HTTP ${response.status}）`;
    let code: string | undefined;
    try {
      const payload = (await response.json()) as {
        detail?: unknown;
        code?: unknown;
      };
      if (typeof payload.code === "string") code = payload.code;
      if (typeof payload.detail === "string" && payload.detail.trim())
        detail = payload.detail;
    } catch {
      /* non-JSON error bodies keep the generic message */
    }
    throw new ApiError(detail, response.status, code);
  }
  try {
    return { status: response.status, data: (await response.json()) as T };
  } catch {
    throw new ApiError("后端返回了无法解析的数据", response.status);
  }
}

export async function request<T>(
  path: string,
  options?: RequestOptions,
): Promise<T> {
  return (await requestWithStatus<T>(path, options)).data;
}

export function newIdempotencyKey(prefix = "web"): string {
  const random =
    typeof globalThis.crypto?.randomUUID === "function"
      ? globalThis.crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${random}`.replace(/[^A-Za-z0-9_-]/g, "").slice(0, 80);
}
