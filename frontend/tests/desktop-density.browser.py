"""Local-only Playwright check. Run against built frontend directories (no API or DB needed).

python tests/desktop-density.browser.py --before path/to/baseline/dist --after dist --out evidence
"""

import argparse
import functools
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

TITLE = ("沪深三百行业中性动量因子与低波动择时策略滚动样本外回测验证" * 2)[:45]
FACTOR = ("自定义多因子研究动量衰减分行业稳定性分析与组合换手率调整" * 2)[:52]
REASON = "数据文件缺少预期字段，工作进程无法读取结构。" * 12 + "故障原因全文末尾"


def fixture(path):
    if path == "/api/v1/data-status":
        return {
            "snapshots": [{"is_subset": False}] * 12,
            "historical_indices": [{}] * 2,
            "market_universes": [{}] * 2,
            "database": {"securities": 5453, "coverage_start": "2020-03-02", "coverage_end": "2026-11-13"},
        }
    if path == "/api/v1/research/tasks":
        return {
            "total": 2,
            "items": [
                {
                    "id": f"task-{status}",
                    "name": name,
                    "kind": "research",
                    "status": status,
                    "stage": "finished" if status == "succeeded" else "backtest",
                    "message": "",
                    "created_at": "2026-11-21T06:00:00Z",
                    "started_at": None,
                    "finished_at": None,
                    "progress": {"completed": 2, "total": 3},
                    "request": {"data": {"mode": "market_warehouse"}, "backtest": {"end": "2026-11-13"}},
                    "result": {"backtest_run_id": "sample", "factor_run_id": "sample"}
                    if status == "succeeded"
                    else None,
                    "error": REASON if status == "failed" else None,
                }
                for status, name in [("failed", TITLE), ("succeeded", "短任务标题")]
            ],
        }
    if path not in {"/api/v1/backtests", "/api/v1/evaluations"}:
        return None
    items = []
    for i in range(12):
        common = {
            "id": f"sample-{i:02d}-" + "example-long-result-identifier" * 2,
            "name": TITLE if i == 0 else f"研究样本 {i + 1}",
            "created_at": f"2026-11-{21 - i:02d}T06:00:00Z",
            "is_synthetic": True,
        }
        if path.endswith("backtests"):
            items.append(
                {
                    **common,
                    "strategy": "DynamicUniverseTopkStrategy",
                    "status": "succeeded",
                    "start_date": "2020-03-02",
                    "end_date": "2026-11-13",
                    "metrics": None if i == 1 else {"total_return": 0 if i == 0 else 0.1234, "max_drawdown": -0.0625},
                }
            )
        else:
            items.append(
                {
                    **common,
                    "start": "2020-03-02",
                    "end": "2026-11-13",
                    "sample_size": 5453,
                    "data_version": "sample",
                    "factor_run_id": "sample",
                    "provenance": {},
                    "evaluation": {"horizons": [1, 5, 20], "quantiles": 5},
                    "factor_config": [
                        {"name": FACTOR if i == 0 else "momentum", "window": 20, "weight": 1},
                        {"name": "volatility", "window": 10, "weight": 0.5},
                        {"name": "mcap", "weight": 0.5},
                    ],
                }
            )
    return {"items": items, "total": len(items)}


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            payload = fixture(path)
            data = json.dumps(payload).encode()
            self.send_response(200 if payload is not None else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if not Path(self.translate_path(path)).is_file():
            self.path = "/index.html"
        super().do_GET()

    def log_message(self, *_):
        pass


def measure(page):
    return page.evaluate("""() => {
      const region = document.querySelector('.table-scroll');
      const rows = Array.from(region?.querySelectorAll('.ant-table-tbody > tr[data-row-key]') || []);
      const rects = rows.map(r => r.getBoundingClientRect());
      const cards = Array.from(document.querySelectorAll('.grid-4 > .ant-card'));
      const box = el => el.getBoundingClientRect();
      return {
        document: document.documentElement.scrollWidth,
        table: region ? [region.scrollWidth, region.clientWidth] : null,
        rows: rects.filter(r => r.bottom <= innerHeight && r.top >= 0).length,
        heights: rects.slice(0, 2).map(r => Math.round(r.height)),
        titleWidth: rows[0] ? Math.round(box(rows[0].cells[0]).width) : null,
        cards: cards.map(c => [Math.round(box(c).top), Math.round(box(c).height)]),
        mobile: document.querySelectorAll('.list-card').length,
      };
    }""")


def capture(browser, dist, out, *, assert_candidate):
    with ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=dist)) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        results = {}
        try:
            for width, height in [(1440, 900), (1366, 768), (1024, 900), (390, 844), (320, 568)]:
                context = browser.new_context(viewport={"width": width, "height": height}, timezone_id="Asia/Shanghai")
                context.route("**/*", lambda r: r.continue_() if r.request.url.startswith(origin + "/") else r.abort())
                page = context.new_page()
                for path in ["/backtests", "/evaluations", "/"]:
                    label = path.strip("/") or "dashboard"
                    page.goto(origin + path, wait_until="networkidle")
                    if path == "/":
                        page.locator(".grid-4").get_by_text("5,453").wait_for()
                    elif width < 768:
                        page.get_by_text(TITLE, exact=True).first.wait_for()
                    else:
                        page.locator(".table-scroll tr[data-row-key]").first.wait_for()
                    numbers = measure(page)
                    results[f"{width}/{label}"] = numbers
                    page.screenshot(path=str(out / f"{width}-{label}.png"), animations="disabled")
                    if not assert_candidate:
                        continue
                    assert numbers["document"] == width, (width, label, numbers)
                    if width < 768:
                        continue
                    if path == "/":
                        assert len({top for top, _ in numbers["cards"]}) == 1, numbers
                        assert max(h for _, h in numbers["cards"]) <= 148, numbers
                        assert numbers["table"][0] == numbers["table"][1], numbers
                        link = page.locator(".dashboard-recent-tasks .task-row-meta").get_by_role("link", name="回测")
                        assert link.bounding_box()["height"] >= 32
                        button = page.get_by_role("button", name="查看完整原因")
                        reason = page.locator(".task-reason-content")
                        assert reason.evaluate("e => e.scrollHeight > e.clientHeight")
                        assert button.bounding_box()["height"] >= 32
                        button.focus()
                        page.keyboard.press("Enter")
                        assert page.get_by_role("button", name="收起原因").get_attribute("aria-expanded") == "true"
                        assert reason.evaluate("e => e.scrollHeight <= e.clientHeight")
                        assert "故障原因全文末尾" in reason.inner_text()
                        if width == 1024:
                            page.screenshot(path=str(out / "1024-dashboard-keyboard.png"), animations="disabled")
                        continue
                    assert numbers["table"][0] == numbers["table"][1], (width, label, numbers)
                    assert numbers["rows"] >= 8, (width, label, numbers)
                    assert numbers["titleWidth"] >= (220 if width == 1024 else 280), (width, label, numbers)
                    link = page.get_by_role("link", name=TITLE, exact=True)
                    link.focus()
                    page.keyboard.press("Shift+Tab")
                    page.keyboard.press("Tab")
                    assert link.evaluate("e => e.matches(':focus-visible')")
                    title_text = link.locator("span")
                    assert title_text.evaluate("e => e.scrollWidth <= e.clientWidth && e.clientHeight >= 20")
                    assert link.bounding_box()["height"] >= 32
                    first_row = page.locator(".ant-table-tbody > tr[data-row-key]").first
                    meta = first_row.locator(".result-list-meta")
                    assert meta.evaluate("e => getComputedStyle(e).fontSize") == "13px"
                    assert (
                        first_row.locator(".result-list-number").first.evaluate(
                            "e => getComputedStyle(e).fontVariantNumeric"
                        )
                        == "tabular-nums"
                    )
                    if path == "/evaluations":
                        button = first_row.get_by_role("button", name="+2 个因子")
                        assert button.bounding_box()["height"] >= 32
                        button.focus()
                        page.keyboard.press("Enter")
                        assert first_row.get_by_role("button", name="收起因子").get_attribute("aria-expanded") == "true"
                        first_row.get_by_text(FACTOR + " 20日", exact=True).wait_for()
                        assert page.url == origin + path
                        assert measure(page)["table"] == numbers["table"]
                        if width == 1024:
                            page.screenshot(path=str(out / "1024-evaluations-keyboard.png"), animations="disabled")
                    else:
                        assert first_row.get_by_text("0.00%", exact=True).count() == 1
                        assert (
                            page.locator(".ant-table-tbody > tr[data-row-key]")
                            .nth(1)
                            .get_by_text("—", exact=True)
                            .count()
                            == 2
                        )
                    href = link.get_attribute("href")
                    link.focus()
                    page.keyboard.press("Enter")
                    page.wait_for_url(origin + href)
                context.close()
        finally:
            server.shutdown()
            thread.join()
        return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        result = {}
        for name, dist in [("before", args.before), ("after", args.after)]:
            out = args.out / name
            out.mkdir(parents=True, exist_ok=True)
            result[name] = capture(browser, dist.resolve(), out, assert_candidate=name == "after")
        browser.close()
    (args.out / "metrics.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
