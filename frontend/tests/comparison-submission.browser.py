"""Browser-only comparison submission acceptance against local TCP HTTP fixtures.

From frontend: npm ci; npm run build; python -m playwright install chromium
python tests/comparison-submission.browser.py --dist dist --out compare-evidence
Requires Python playwright and Node; imports the existing frontend options fixture.
"""

import argparse
import functools
import json
import socket
import subprocess
import threading
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

OPTIONS = None
ENTRY = "/experiments/new"
EXPERIMENTS = "/api/v1/research/experiments"
TASKS = "/api/v1/research/tasks"
UNKNOWN = "是否受理尚未确认"
NAMES = {"split": "提交方案比较", "comparison": "运行收益对照"}
WIDTHS = [(1440, 900), (1366, 768), (1024, 900), (390, 844), (320, 568)]


class Fixture:
    def __init__(self, modes):
        self.modes = modes
        self.lock = threading.Lock()
        self.calls = []
        self.cancels = []
        self.accepted = {}
        self.pending = [threading.Event() for _ in modes]
        self.released = [threading.Event() for _ in modes]
        self.finished = [threading.Event() for _ in modes]

    def post(self, key, body):
        with self.lock:
            if not key:
                raise AssertionError("Idempotency-Key header missing")
            i = len(self.calls)
            self.calls.append({"key": key, "body": body})
            mode = self.modes[i] if i < len(self.modes) else "unexpected"
            if mode in {"422", "409", "503", "504", "unexpected"}:
                return i, mode, None
            if key in self.accepted and self.accepted[key]["request"] != body:
                raise AssertionError("One key was used for two different payloads")
            task = self.accepted.setdefault(
                key,
                {
                    "id": f"accepted-{len(self.accepted) + 1:02d}",
                    "name": body["name"],
                    "kind": "experiment",
                    "status": "queued",
                    "stage": "queued",
                    "created_at": "2025-07-01T00:00:00Z",
                    "started_at": None,
                    "finished_at": None,
                    "progress": {},
                    "message": "",
                    "error": None,
                    "result": None,
                    "request": body,
                },
            )
            return i, mode, task


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, fixture, **kwargs):
        self.fixture = fixture
        super().__init__(*args, **kwargs)

    def log_message(self, *_):
        pass

    def json(self, data, status=200):
        self.bytes(json.dumps(data, ensure_ascii=False).encode(), status)

    def bytes(self, data, status):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/v1/research/options":
                self.json(OPTIONS)
            elif path == TASKS:
                with self.fixture.lock:
                    items = list(self.fixture.accepted.values())
                self.json({"items": items, "total": len(items)})
            elif path.startswith(TASKS + "/"):
                with self.fixture.lock:
                    items = list(self.fixture.accepted.values())
                match = next((t for t in items if path.endswith("/" + t["id"])), None)
                self.json(match if match else {"detail": "task not found"}, 200 if match else 404)
            elif path.startswith("/api/"):
                self.json({"detail": "fixture route not present"}, 404)
            else:
                if not Path(self.translate_path(path)).is_file():
                    self.path = "/index.html"
                super().do_GET()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def do_POST(self):
        path = urlparse(self.path).path
        if path.endswith("/cancel"):
            self.fixture.cancels.append(path)
            self.json({"detail": "cancellation must not be called"}, 400)
            return
        if path != EXPERIMENTS:
            self.json({"detail": "fixture route not present"}, 404)
            return
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        i, mode, task = self.fixture.post(self.headers.get("Idempotency-Key"), json.loads(raw))
        if mode == "unexpected":
            self.json({"detail": "unexpected duplicate POST"}, 500)
            return
        self.fixture.pending[i].set()
        try:
            if mode in {"hold", "hold-bad"}:
                assert self.fixture.released[i].wait(30), "script did not release held response"
            if mode == "drop":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.close_connection = True
            elif mode in {"bad-json", "hold-bad"}:
                self.bytes(b'{"id":', 202)
            elif mode == "missing-id":
                self.json({"name": task["name"]}, 202)
            elif mode in {"422", "409", "503", "504"}:
                details = {"422": "方案校验：样本不足", "409": "该标识已关联不同参数"}
                self.json({"detail": details.get(mode, "网关无法给出受理结果")}, int(mode))
            else:
                self.json(task, 202)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass
        finally:
            self.fixture.finished[i].set()


@contextmanager
def serve(dist, modes):
    fixture = Fixture(modes)
    handler = functools.partial(Handler, directory=dist, fixture=fixture)
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", fixture
        finally:
            for event in fixture.released:
                event.set()
            server.shutdown()
            thread.join()


def activate(locator, mode):
    if mode == "touch":
        locator.tap()
    elif mode == "keyboard":
        locator.focus()
        locator.press("Enter")
    else:
        locator.click()


def primary(page, entry):
    return page.get_by_role("button", name=NAMES[entry]).filter(visible=True)


def input_named(page, value):
    return page.locator("input").filter(visible=True).and_(page.locator(f'input[value="{value}"]'))


def prepare(page, origin, entry, mode):
    path = ENTRY + ("?mode=comparison" if entry == "comparison" else "")
    page.goto(origin + path)
    expect(primary(page, entry)).to_be_visible()
    if entry == "split":
        dates = page.locator(".ant-card").filter(has=page.get_by_text("分段", exact=True)).locator(".ant-picker input")
        assert dates.count() == 2
        for control, value in zip(dates.all(), ("2025-04-01", "2025-05-15"), strict=True):
            control.fill(value)
            control.press("Enter")
            expect(control).to_have_value(value)
        source = "方案 A"
    else:
        common = page.get_by_role("checkbox", name="固定相同股票样本")
        if mode == "keyboard":
            common.focus()
            common.press("Space")
        else:
            activate(common, mode)
        expect(common).not_to_be_checked()
        source = "基准方案"
    editor = input_named(page, source)
    expect(editor).to_have_count(1)
    editor.fill("独立验收方案 A")
    expect(primary(page, entry)).to_be_enabled()
    return origin + path


def assert_payload(call, entry):
    plan = call["body"]["plan"]
    assert plan["candidates"][0]["name"] == "独立验收方案 A", plan
    if entry == "split":
        assert plan["periods"]["validation"]["start"] == "2025-04-01"
        assert plan["periods"]["test"]["start"] == "2025-05-15"
    else:
        assert plan["comparison"]["sample_mode"] == "independent"


def assert_pending(fixture, i):
    assert fixture.pending[i].wait(5), fixture.calls


def unknown(page):
    expect(page.get_by_text(UNKNOWN, exact=True)).to_be_visible()
    expect(page.get_by_text("若任务已受理，它仍会继续。", exact=False)).to_be_visible()


def layout(locator, page, width):
    locator.scroll_into_view_if_needed()
    box = locator.bounding_box()
    assert box and box["height"] >= 32 and box["x"] >= 0 and box["x"] + box["width"] <= width + 1, box
    assert page.evaluate("document.documentElement.scrollWidth") == width


def text_contrast(locator):
    # Resolve translucent text against the actual ancestor backgrounds, then
    # use the WCAG sRGB relative luminance ratio for normal-size text.
    return locator.evaluate("""element => {
        const rgba = color => {
            const match = /^rgba?\\((.+)\\)$/.exec(color);
            if (!match) throw new Error(`Expected computed sRGB: ${color}`);
            const [r, g, b, a = 1] = match[1].split(',').map(Number);
            return [r, g, b, a];
        };
        const over = ([r, g, b, a], below) =>
            [r, g, b].map((channel, i) => channel * a + below[i] * (1 - a));
        const layers = [];
        for (let node = element; node; node = node.parentElement) {
            const color = rgba(getComputedStyle(node).backgroundColor);
            layers.push(color);
            if (color[3] === 1) break;
        }
        if (layers.at(-1)[3] !== 1) throw new Error('No opaque background found');
        const background = layers.reverse().reduce((below, top) => over(top, below), [0, 0, 0]);
        const color = getComputedStyle(element).color;
        const foreground = over(rgba(color), background);
        const luminance = rgb => rgb.map(value => {
            const s = value / 255;
            return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
        }).reduce((sum, value, i) => sum + value * [0.2126, 0.7152, 0.0722][i], 0);
        const [lo, hi] = [luminance(foreground), luminance(background)].sort((a, b) => a - b);
        return {color, background, ratio: (hi + 0.05) / (lo + 0.05)};
    }""")


def stop_case(browser, dist, out, width, height, entry, *, theme="light"):
    mode = "touch" if width < 768 else "keyboard" if width == 1366 else "mouse"
    context = browser.new_context(
        viewport={"width": width, "height": height}, has_touch=mode == "touch", color_scheme=theme
    )
    try:
        with serve(dist, ["hold", "hold"]) as (origin, fixture):
            page = context.new_page()
            url = prepare(page, origin, entry, mode)
            expect(page.locator("html")).to_have_attribute("data-theme", theme)
            submit = primary(page, entry)
            submit.scroll_into_view_if_needed()
            if mode == "mouse":
                box = submit.bounding_box()
                page.mouse.dblclick(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            else:
                activate(submit, mode)
            assert_pending(fixture, 0)
            stop = page.get_by_role("button", name="停止等待")
            help_text = page.get_by_text("这不会取消任务。", exact=False)
            expect(stop).to_have_accessible_description(
                "等待确认时参数不可编辑。停止等待后可继续编辑，这不会取消任务。"
            )
            # Check both rectangles before any scroll or action by the test.
            expect(stop).to_be_in_viewport(ratio=1)
            expect(help_text).to_be_in_viewport(ratio=1)
            contrast = text_contrast(help_text)
            assert contrast["ratio"] >= 4.5, (width, entry, theme, contrast)
            if mode == "touch":
                page.screenshot(path=str(out / f"{width}-{entry}-{theme}-waiting.png"), animations="disabled")
            layout(stop, page, width)
            if mode == "keyboard":
                expect(stop).to_be_focused()
                page.keyboard.press("Space")
            else:
                activate(stop, mode)
            unknown(page)
            submit = page.get_by_role("button", name="按原参数重试")
            expect(input_named(page, "独立验收方案 A")).to_have_value("独立验收方案 A")
            if mode == "keyboard":
                expect(submit).to_be_focused()
            assert len(fixture.calls) == 1
            assert len(fixture.accepted) == 1
            assert_no_cancel(fixture)
            task_link = page.get_by_role("link", name="任务中心（新标签页）")
            layout(task_link, page, width)
            assert task_link.evaluate("""e => e.getBoundingClientRect().right <=
              e.closest('.ant-alert').getBoundingClientRect().right""")
            expect(submit).to_be_enabled()
            if width in (1024, 320) and theme == "light":
                page.screenshot(path=str(out / f"{width}-{entry}-unconfirmed.png"), animations="disabled")
            with context.expect_page() as popup:
                activate(task_link, mode)
            task_page = popup.value
            task_page.wait_for_url(origin + "/tasks")
            expect(
                task_page.get_by_text(fixture.accepted[fixture.calls[0]["key"]]["name"], exact=True).first
            ).to_be_visible()
            task_page.close()
            assert page.url == url
            expect(input_named(page, "独立验收方案 A")).to_have_value("独立验收方案 A")
            activate(submit, mode)
            assert_pending(fixture, 1)
            fixture.released[0].set()
            assert fixture.finished[0].wait(5)
            expect(stop).to_be_visible()
            assert page.url == url
            fixture.released[1].set()
            page.wait_for_url(origin + "/tasks?focus=accepted-01")
            assert fixture.calls[0] == fixture.calls[1]
            assert len(fixture.accepted) == 1
            assert_no_cancel(fixture)
            assert_payload(fixture.calls[0], entry)
            return {
                "viewport": width,
                "entry": entry,
                "input": mode,
                "theme": theme,
                "waiting_help_contrast": contrast,
                "posts": 2,
                "accepted": 1,
                "result": "accepted-01",
            }
    finally:
        context.close()


def assert_no_cancel(fixture):
    assert fixture.cancels == [], fixture.cancels


def transport_case(browser, dist, entry, failure):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    try:
        with serve(dist, [failure, "ok"]) as (origin, fixture):
            page = context.new_page()
            url = prepare(page, origin, entry, "mouse")
            if failure == "hold":
                page.clock.install()
            primary(page, entry).click()
            assert_pending(fixture, 0)
            if failure == "hold":
                expect(page.get_by_role("button", name="停止等待")).to_be_visible()
                page.clock.fast_forward(31_000)
                expect(page.get_by_text("等待超时。", exact=False)).to_be_visible()
            if failure in {"409", "422"}:
                expect(page.get_by_text("提交未受理", exact=True)).to_be_visible()
                expect(
                    page.get_by_text("方案校验：样本不足" if failure == "422" else "该标识已关联不同参数", exact=True)
                ).to_be_visible()
                expect(page.get_by_text(UNKNOWN, exact=True)).to_have_count(0)
            else:
                unknown(page)
            assert page.url == url
            expect(input_named(page, "独立验收方案 A")).to_have_value("独立验收方案 A")
            retry = (
                primary(page, entry) if failure in {"409", "422"} else page.get_by_role("button", name="按原参数重试")
            )
            retry.click()
            page.wait_for_url(origin + "/tasks?focus=accepted-01")
            assert fixture.calls[0] == fixture.calls[1], fixture.calls
            assert len(fixture.accepted) == 1
            assert_no_cancel(fixture)
            return {"entry": entry, "first": failure, "posts": 2, "accepted": 1}
    finally:
        context.close()


def locked_case(browser, dist, entry, *, stop_first):
    with serve(dist, ["hold"]) as (origin, fixture):
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        try:
            url = prepare(page, origin, entry, "mouse")
            primary(page, entry).click()
            assert_pending(fixture, 0)
            editor = input_named(page, "独立验收方案 A")
            # Real pointer and keyboard events must not edit A while its response is held.
            editor.scroll_into_view_if_needed()
            box = editor.bounding_box()
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.keyboard.insert_text("等待中修改的 B")
            expect(editor).to_have_value("独立验收方案 A")
            expect(editor).to_be_disabled()
            group = page.get_by_role("group", name="比较参数")
            for control in group.locator("input:visible, button:visible, [role=combobox]:visible").all():
                expect(control).to_be_disabled()
            expect(page.get_by_text("停止等待后可继续编辑", exact=False)).to_be_visible()
            stop = page.get_by_role("button", name="停止等待")
            expect(stop).to_be_enabled()
            if stop_first:
                stop.click()
                unknown(page)
                expect(editor).to_be_editable()
                editor.fill("修改后的验收方案")
            fixture.released[0].set()
            assert fixture.finished[0].wait(5)
            if stop_first:
                expect(input_named(page, "修改后的验收方案")).to_have_value("修改后的验收方案")
                assert page.url == url
            else:
                page.wait_for_url(origin + "/tasks?focus=accepted-01")
            assert len(fixture.calls) == len(fixture.accepted) == 1
            assert_payload(fixture.calls[0], entry)
            assert_no_cancel(fixture)
            return {
                "entry": entry,
                "first": "locked-stop-edit" if stop_first else "locked-success",
                "posts": 1,
                "accepted": 1,
            }
        finally:
            page.close()


def success_case(browser, dist, entry):
    with serve(dist, ["ok"]) as (origin, fixture):
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        try:
            prepare(page, origin, entry, "mouse")
            primary(page, entry).click()
            page.wait_for_url(origin + "/tasks?focus=accepted-01")
            assert len(fixture.calls) == len(fixture.accepted) == 1
            assert_payload(fixture.calls[0], entry)
            assert_no_cancel(fixture)
            return {"entry": entry, "first": "ok", "posts": 1, "accepted": 1}
        finally:
            page.close()


def changed_case(browser, dist, entry):
    with serve(dist, ["hold-bad", "hold", "ok"]) as (origin, fixture):
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        try:
            page = context.new_page()
            url = prepare(page, origin, entry, "mouse")
            primary(page, entry).click()
            assert_pending(fixture, 0)
            page.get_by_role("button", name="停止等待").click()
            unknown(page)
            input_named(page, "独立验收方案 A").fill("修改后的验收方案")
            expect(page.get_by_text("当前参数已改动，提交可能创建另一条任务。", exact=False)).to_be_visible()
            primary(page, entry).click()
            assert_pending(fixture, 1)
            fixture.released[0].set()
            assert fixture.finished[0].wait(5)
            expect(page.get_by_role("button", name="停止等待")).to_be_visible()
            assert page.url == url
            page.get_by_role("button", name="停止等待").click()
            unknown(page)
            input_named(page, "修改后的验收方案").fill("独立验收方案 A")
            primary(page, entry).click()
            page.wait_for_url(origin + "/tasks?focus=accepted-01")
            assert fixture.calls[0] == fixture.calls[2]
            assert fixture.calls[1]["key"] != fixture.calls[0]["key"]
            assert len(fixture.accepted) == 2
            assert_no_cancel(fixture)
            return {"entry": entry, "first": "changed-and-reverted", "posts": 3, "accepted": 2}
        finally:
            context.close()


def main():
    global OPTIONS
    expect.set_options(timeout=12_000)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    fixture_uri = Path(__file__).with_name("fixtures.mjs").resolve().as_uri()
    OPTIONS = json.loads(
        subprocess.check_output(
            [
                "node",
                "--input-type=module",
                "-e",
                f"import {{options}} from {json.dumps(fixture_uri)}; console.log(JSON.stringify(options()))",
            ],
            encoding="utf-8",
        )
    )
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for width, height in WIDTHS:
            for entry in NAMES:
                results.append(stop_case(browser, args.dist.resolve(), args.out, width, height, entry))
                if width < 768:
                    results.append(
                        stop_case(browser, args.dist.resolve(), args.out, width, height, entry, theme="dark")
                    )
        for entry in NAMES:
            for failure in ("drop", "503", "504", "422", "409", "bad-json", "missing-id", "hold"):
                results.append(transport_case(browser, args.dist.resolve(), entry, failure))
            results.append(changed_case(browser, args.dist.resolve(), entry))
            results.append(success_case(browser, args.dist.resolve(), entry))
            for stop_first in (False, True):
                results.append(locked_case(browser, args.dist.resolve(), entry, stop_first=stop_first))
        browser.close()
    (args.out / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"{len(results)} Chromium / TCP fixture cases passed")


if __name__ == "__main__":
    main()
