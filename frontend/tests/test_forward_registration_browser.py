"""Run explicitly with pytest, a built frontend, Playwright, and isolated PostgreSQL.

For screenshots, set FINANCE_QUANT_BROWSER_EVIDENCE to an empty output directory.
Set FINANCE_QUANT_BROWSER_CHANNEL=msedge to use an installed Microsoft Edge.
"""

from __future__ import annotations

import os
import re
import secrets
import socket
import sys
import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "tests" / "integration")]

from test_forward_read_api_postgres import _stored, historical_attempts  # noqa: E402
from test_forward_score_postgres import CODES, _execute, _fixture  # noqa: E402
from test_forward_verification_postgres import _publish_labels, _registered  # noqa: E402
from test_postgres_runtime import engine as engine  # noqa: E402

from finance_quant.api import create_app  # noqa: E402
from finance_quant.application.forward_score import enqueue_forward_score  # noqa: E402
from finance_quant.application.forward_verification import enqueue_forward_verification  # noqa: E402
from finance_quant.application.tasks import TaskService  # noqa: E402
from finance_quant.persistence.forward import registration_evidence_digest, start_forward_attempt  # noqa: E402
from finance_quant.persistence.results import PgResultStore  # noqa: E402
from finance_quant.persistence.schema import forward_registration  # noqa: E402
from finance_quant.persistence.tasks import PgTaskStore  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("FINANCE_QUANT_TEST_POSTGRES") != "1", reason="requires isolated PG")
REGISTRATIONS = "/forward-registrations"
API = "/api/v1" + REGISTRATIONS
TASKS = "/api/v1/research/tasks"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        channel = os.environ.get("FINANCE_QUANT_BROWSER_CHANNEL")
        with playwright.chromium.launch(channel=channel) as chromium:
            yield chromium


@contextmanager
def serve(engine, root):
    dist = ROOT / "frontend" / "dist"
    assert (dist / "index.html").is_file(), "Build the frontend first"
    app = create_app(
        result_store=PgResultStore(root / "runs", engine=engine, storage_root=root),
        task_service=TaskService(root / "runs", root, task_store=PgTaskStore(engine=engine)),
        frontend_dist=dist,
    )
    with socket.socket() as listener:
        # Chromium rejects some ports that Windows can assign with bind(..., 0).
        for _ in range(20):
            try:
                listener.bind(("127.0.0.1", 49152 + secrets.randbelow(16384)))
                break
            except OSError:
                continue
        else:
            pytest.fail("Could not reserve a loopback browser test port")
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.started
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=15)
            assert not thread.is_alive()


def screen(page, name, *, full_page=True):
    output = os.environ.get("FINANCE_QUANT_BROWSER_EVIDENCE")
    if output:
        destination = Path(output)
        destination.mkdir(parents=True, exist_ok=True)
        if full_page:
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_function("window.scrollY === 0")
        page.screenshot(path=destination / f"{name}.png", full_page=full_page, animations="disabled")


def assert_fits(page):
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert page.evaluate("""[...document.querySelectorAll('.forward-page .ant-card')].every(card =>
        card.scrollWidth <= card.clientWidth + 1)""")


def assert_status_contrast(page, scheme, width):
    page.wait_for_function("""() => [...document.querySelectorAll('.forward-page .ant-tag')]
        .every(tag => tag.getAnimations().length === 0)""")
    samples = page.locator(".forward-page .ant-tag").evaluate_all("""tags => {
        const canvas = Object.assign(document.createElement('canvas'), {width: 1, height: 1});
        const context = canvas.getContext('2d', {willReadFrequently: true});
        const luminance = ([r, g, b]) => [r, g, b].map(channel => {
            const value = channel / 255;
            return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
        }).reduce((sum, value, index) => sum + value * [0.2126, 0.7152, 0.0722][index], 0);
        const paint = color => {
            context.fillStyle = color;
            context.fillRect(0, 0, 1, 1);
        };
        return tags.map(tag => {
            const layers = [];
            for (let element = tag; element; element = element.parentElement)
                layers.push(getComputedStyle(element).backgroundColor);
            paint('#fff');
            layers.reverse().forEach(paint);
            const background = [...context.getImageData(0, 0, 1, 1).data].slice(0, 3);
            paint(getComputedStyle(tag).color);
            const foreground = [...context.getImageData(0, 0, 1, 1).data].slice(0, 3);
            const values = [luminance(background), luminance(foreground)].sort((a, b) => a - b);
            return {label: tag.textContent.trim(), foreground, background,
                ratio: (values[1] + 0.05) / (values[0] + 0.05)};
        });
    }""")
    for sample in samples:
        print(f"contrast {scheme=} {width=} {sample}")
        assert sample["ratio"] >= 4.5, sample


def activate(page, link, mobile):
    if mobile:
        assert link.bounding_box()["height"] >= 44
        link.tap()
    else:
        link.focus()
        page.keyboard.press("Shift+Tab")
        page.keyboard.press("Tab")
        expect(link).to_be_focused()
        page.keyboard.press("Enter")


def expect_restored(page, link):
    expect(link).to_be_focused()
    top = link.bounding_box()["y"]
    assert top >= page.locator(".app-header").bounding_box()["height"]


def test_forward_navigation_and_five_viewports(engine, tmp_path, browser):
    row, score, verify = historical_attempts(engine, tmp_path)
    long_name = "长期样本外动量前瞻验证登记" * 5
    with engine.begin() as connection:
        saved = dict(connection.execute(select(forward_registration)).mappings().one())
        for i in range(22):
            clone = dict(
                saved,
                id=f"fwd-list-fixture-{i}",
                name=f"{long_name} {i + 1}",
                idempotency_key=f"list-fixture-{i}",
                score_snapshot=None,
                label_snapshot=None,
                result=None,
                result_recorded_at=None,
                created_at=saved["created_at"] + timedelta(seconds=i + 1),
            )
            clone["confirmed_evidence_sha256"] = registration_evidence_digest(clone)
            connection.execute(forward_registration.insert().values(**clone))
    before = _stored(engine)

    with serve(engine, tmp_path) as origin:
        for width in (1440, 1366, 1024, 390, 320):
            mobile = width <= 390
            with browser.new_context(
                viewport={"width": width, "height": 900 if not mobile else 800},
                is_mobile=mobile,
                has_touch=mobile,
                timezone_id="America/Los_Angeles",
                reduced_motion="reduce",
                color_scheme="light",
            ) as context:
                page = context.new_page()
                errors, requests = [], []
                page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
                page.on(
                    "request",
                    lambda request, requests=requests: requests.append((request.method, urlsplit(request.url))),
                )
                page.goto(origin + REGISTRATIONS)
                expect(page.get_by_text("共 23 条登记")).to_be_visible()
                assert [url.query for _, url in requests if url.path == API] == ["offset=0&limit=20"]
                assert not any(url.path.startswith(API + "/") for _, url in requests)
                expect(
                    page.locator(".forward-mobile-list" if mobile else ".forward-registration-table")
                ).to_be_visible()
                if width == 1024:
                    expect(page.get_by_role("columnheader", name="登记日期")).to_be_visible()
                assert_fits(page)
                page.locator(".ant-pagination-next button").click()
                page.wait_for_url(origin + REGISTRATIONS + "?page=2")
                first = page.get_by_role("link", name=row["name"], exact=True)
                expect(first).to_be_visible()
                assert any(url.path == API and url.query == "offset=20&limit=20" for _, url in requests)
                screen(page, f"list-{width}")
                activate(page, first, mobile)
                expect(page.get_by_text("0.00 个百分点", exact=True)).to_be_visible()
                expect(page.get_by_text("已封存验证结果", exact=True)).to_be_visible()
                expected_date = row["exit_date"].isoformat() + " 15:00:00"
                expect(page.get_by_text(expected_date, exact=True).first).to_be_visible()
                expect(page.get_by_text("无关联任务", exact=True)).to_have_count(2)
                assert page.get_by_role("link", name="无关联任务").count() == 0
                audit = page.locator("summary", has_text="审计信息")
                if mobile:
                    assert audit.bounding_box()["height"] >= 44
                audit.click()
                expect(page.get_by_text("计算源码指纹", exact=True)).to_be_visible()
                assert_fits(page)
                audit.click()
                screen(page, f"detail-{width}")

                for receipt, name, stage in (
                    (score, "查看评分任务 · 第 1 次", "前瞻评分"),
                    (verify, "查看到期验证任务 · 第 2 次", "到期验证"),
                ):
                    link = page.get_by_role("link", name=name)
                    activate(page, link, mobile)
                    page.wait_for_url(origin + "/tasks?focus=" + receipt["task_id"])
                    target = page.locator(".forward-task-summary")
                    expect(target.get_by_text(stage, exact=True)).to_be_visible()
                    expect(target.get_by_text(receipt["task_id"], exact=True)).to_be_visible()
                    expect(target.get_by_text(receipt["attempt_id"], exact=True)).to_be_visible()
                    assert any(url.path == TASKS + "/" + receipt["task_id"] for _, url in requests)
                    assert_fits(page)
                    if width in (1440, 390) and receipt is score:
                        screen(page, f"history-{width}", full_page=False)
                    page.go_back()
                    expect_restored(page, link)

                page.go_back()
                page.wait_for_url(origin + REGISTRATIONS + "?page=2")
                expect_restored(page, first)
                assert not errors
                assert {method for method, _ in requests} == {"GET"}
    assert _stored(engine) == before


@pytest.mark.parametrize(
    ("case", "label"),
    [
        ("unsealed", "登记未封存"),
        ("awaiting_score", "待评分"),
        ("awaiting_exit", "等待退出日"),
        ("awaiting_quote", "等待绑定合格标签"),
        ("awaiting_verification", "待验证"),
        ("score_queued", "评分已排队"),
        ("scoring", "正在评分"),
        ("verify_queued", "验证已排队"),
        ("verifying", "正在验证"),
        ("failed", "评分失败"),
        ("verify_failed", "到期验证失败"),
        ("expired", "登记已过期"),
        ("missed", "已错过截止"),
        ("invalid_evidence", "证据无效"),
        ("missing_labels", "缺少收益数据"),
        ("verified", "已封存验证结果"),
    ],
)
def test_details_show_server_states_and_missing_data(engine, tmp_path, browser, case, label):
    if case in {
        "unsealed",
        "awaiting_score",
        "score_queued",
        "scoring",
        "failed",
        "expired",
        "missed",
        "invalid_evidence",
    }:
        identifier, leaf, _ = _fixture(engine, tmp_path, late=case == "missed", missing=case == "failed")
        if case == "invalid_evidence":
            with engine.begin() as connection:
                connection.execute(forward_registration.update().values(confirmed_evidence_sha256="0" * 64))
        elif case in {"unsealed", "expired"}:
            values = {"status": "expired"} if case == "expired" else {"confirmed_at": None}
            with engine.begin() as connection:
                connection.execute(forward_registration.update().values(**values))
        elif case in {"score_queued", "scoring", "failed"}:
            store = PgTaskStore(engine=engine)
            binding = enqueue_forward_score(store, tmp_path, identifier, leaf.name, key="browser-score")
            if case == "scoring":
                assert store.claim() == binding["task_id"]
                with engine.begin() as connection:
                    start_forward_attempt(connection, binding["task_id"], identifier, kind="score")
            elif case == "failed":
                assert _execute(store, tmp_path, binding)[1]["status"] == "failed"
    else:
        store, row, _ = _registered(engine, tmp_path, mature=case != "awaiting_exit")
        identifier = row["id"]
        if case not in {"awaiting_exit", "awaiting_quote"}:
            _publish_labels(tmp_path, row, "browser-label", missing=CODES[-1] if case == "missing_labels" else None)
            binding = enqueue_forward_verification(store, tmp_path, identifier, key="browser-verify")
            if case == "awaiting_verification":
                store.cancel(binding["task_id"])
            elif case in {"verifying", "verify_failed"}:
                assert store.claim() == binding["task_id"]
                with engine.begin() as connection:
                    start_forward_attempt(connection, binding["task_id"], identifier, kind="verify")
                if case == "verify_failed":
                    store.finish(binding["task_id"], "failed", error="verification-execution-failed")
            elif case in {"missing_labels", "verified"}:
                assert _execute(store, tmp_path, binding)[1]["status"] == "succeeded"
    widths = (1440, 1366, 1024, 390, 320) if case in {"failed", "missing_labels"} else (390,)
    with serve(engine, tmp_path) as origin:
        for width in widths:
            with browser.new_context(viewport={"width": width, "height": 800}, color_scheme="light") as context:
                page = context.new_page()
                assert page.goto(f"{origin}{REGISTRATIONS}/{identifier}").status == 200
                for scheme in ("light", "dark"):
                    page.emulate_media(color_scheme=scheme)
                    expect(page.locator("html")).to_have_attribute("data-theme", scheme)
                    expect(page.locator(".forward-outcome .ant-tag")).to_have_text(label)
                    expect(page.locator(".forward-metric-value")).to_have_text(
                        "13.33 个百分点" if case == "verified" else "—"
                    )
                    if case == "missing_labels":
                        expect(page.get_by_text("有效收益数 · 前 K 1/1，全池 2/3", exact=True)).to_be_visible()
                        expect(page.get_by_text("退出缺行情", exact=False)).to_be_visible()
                    if case == "failed":
                        expect(page.get_by_text("评分样本不全", exact=True).first).to_be_visible()
                        expect(page.locator(".forward-missing")).to_contain_text(CODES[-1])
                    assert_status_contrast(page, scheme, width)
                    assert_fits(page)
                    if case in {"awaiting_quote", "failed", "missing_labels"} and width == 390:
                        screen(page, f"state-{case}-{scheme}-390")


def test_empty_not_found_and_retry(engine, tmp_path, browser):
    with serve(engine, tmp_path) as origin, browser.new_context(viewport={"width": 320, "height": 800}) as context:
        page = context.new_page()
        page.route(
            "**/api/v1/forward-registrations?*",
            lambda route: route.fulfill(status=503, json={"detail": "临时读取失败"}),
            times=1,
        )
        page.goto(origin + REGISTRATIONS)
        expect(page.get_by_text("登记列表加载失败", exact=True)).to_be_visible()
        page.get_by_role("button", name=re.compile(r"重\s*试")).click()
        expect(page.get_by_text("暂无前瞻登记", exact=True)).to_be_visible()
        page.route(
            f"**{API}/unknown", lambda route: route.fulfill(status=503, json={"detail": "临时读取失败"}), times=1
        )
        page.goto(origin + REGISTRATIONS + "/unknown")
        expect(page.get_by_text("详情加载失败", exact=True)).to_be_visible()
        page.get_by_role("button", name=re.compile(r"重\s*试")).click()
        expect(page.get_by_text("登记不存在", exact=True)).to_be_visible()
        expect(page.get_by_role("link", name="返回登记列表")).to_be_visible()

        identifier = "task-" + "a" * 32
        page.route(
            f"**{TASKS}/{identifier}", lambda route: route.fulfill(status=503, json={"detail": "临时读取失败"}), times=1
        )
        page.goto(f"{origin}/tasks?focus={identifier}")
        expect(page.get_by_text("任务加载失败", exact=True)).to_be_visible()
        page.get_by_role("button", name=re.compile(r"重\s*试")).click()
        expect(page.get_by_text("任务不可用", exact=True)).to_be_visible()


def ordinary_task(index):
    return {
        "id": f"task-{index:032x}",
        "kind": "research",
        "name": f"普通研究 {index:02}",
        "status": "running",
        "stage": "prepare",
        "message": "正在准备数据",
        "created_at": "2026-11-20T12:00:00Z",
        "started_at": "2026-11-20T12:01:00Z",
        "finished_at": None,
        "progress": {"completed": 0, "total": 2},
        "request": {
            "data": {"mode": "snapshot", "codes": ["000001.SZ"]},
            "backtest": {"start": "2026-11-01", "end": "2026-11-20", "topk": 1, "initial_cash": 10000},
            "factors": [],
        },
        "error": None,
        "result": None,
    }


@pytest.mark.parametrize("refresh", ["button", "poll"])
def test_historical_task_refreshes_by_id(engine, tmp_path, browser, refresh):
    task = ordinary_task(101)
    task.update(
        forward_stage="score",
        forward_attempt_id="fat-historical",
        request={"registration_id": "fwd-history", "score_snapshot_id": "synthetic-score"},
    )

    with serve(engine, tmp_path) as origin, browser.new_context() as context:
        page = context.new_page()
        if refresh == "button":
            # Prove the button reaches the detail even when automatic task polling is absent.
            page.add_init_script("""(() => {
                const schedule = window.setInterval.bind(window);
                window.setInterval = (callback, delay, ...args) =>
                    delay === 3000 ? 0 : schedule(callback, delay, ...args);
            })()""")
        else:
            page.clock.install()
        page.route(f"**{TASKS}/{task['id']}", lambda route: route.fulfill(json=task))
        page.goto(f"{origin}/tasks?focus={task['id']}")
        status = page.locator(".section-card", has=page.get_by_text("指定任务", exact=True)).locator(".ant-tag")
        expect(status).to_have_text("运行中")
        task.update(status="succeeded", stage="finished", message="计算完成", finished_at="2026-11-20T12:02:00Z")
        if refresh == "button":
            page.get_by_role("button", name=re.compile(r"刷\s*新")).click(timeout=2000)
        else:
            page.clock.fast_forward(3100)
        expect(status).to_have_text("已完成", timeout=2000)


def test_task_poll_keeps_user_page_after_initial_focus(engine, tmp_path, browser):
    tasks = [ordinary_task(index) for index in range(70)]
    first, next_focus = tasks[5]["id"], tasks[28]["id"]

    with serve(engine, tmp_path) as origin, browser.new_context() as context:
        page = context.new_page()
        page.clock.install()
        page.route(f"**{TASKS}", lambda route: route.fulfill(json={"items": tasks, "total": len(tasks)}))
        page.goto(f"{origin}/tasks?focus={first}")
        active_page = page.locator(".task-list-wrap .ant-pagination-item-active")
        expect(active_page).to_have_text("1")
        expect(page.locator(f"#task-detail-{first}")).to_be_visible()
        page.locator('.task-list-wrap .ant-pagination-item[title="3"]').click()
        expect(page.locator(".task-row").first).to_have_attribute("data-task-id", tasks[40]["id"])
        tasks.insert(0, ordinary_task(100))
        page.clock.fast_forward(3100)
        expect(page.locator(".task-row").first).to_have_attribute("data-task-id", tasks[40]["id"])
        expect(active_page).to_have_text("3")

        page.evaluate(
            """id => {
                history.pushState(null, '', `/tasks?focus=${id}`);
                dispatchEvent(new PopStateEvent('popstate'));
            }""",
            next_focus,
        )
        expect(active_page).to_have_text("2")
        expect(page.locator(f"#task-detail-{next_focus}")).to_be_visible()
