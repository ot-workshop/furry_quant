interface PaginationProps {
  total: number;
  offset: number;
  limit: number;
  onChange: (offset: number) => void;
  loading?: boolean;
}

export function Pagination({
  total,
  offset,
  limit,
  onChange,
  loading = false,
}: PaginationProps) {
  const pages = Math.max(1, Math.ceil(total / limit));
  const page = Math.min(pages, Math.floor(offset / limit) + 1);

  return (
    <div className="pagination">
      <span className="pagination-info">
        第 {page} / {pages} 页 · 共 {total.toLocaleString("zh-CN")} 条
      </span>
      <div className="pagination-actions">
        <button
          type="button"
          className="button button-compact"
          disabled={loading || offset <= 0}
          onClick={() => onChange(Math.max(0, offset - limit))}
        >
          上一页
        </button>
        <button
          type="button"
          className="button button-compact"
          disabled={loading || offset + limit >= total}
          onClick={() => onChange(offset + limit)}
        >
          下一页
        </button>
      </div>
    </div>
  );
}
