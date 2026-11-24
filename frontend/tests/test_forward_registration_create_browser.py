"""Exercise the creation form with the built frontend and private PostgreSQL."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from playwright.sync_api import expect
from sqlalchemy import update

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "tests" / "integration")]

from test_forward_register_api_postgres import _rows  # noqa: E402
from test_forward_register_api_postgres import setup_registration as setup_registration  # noqa: E402
from test_forward_registration_browser import (  # noqa: E402
    API,
    REGISTRATIONS,
    activate,
    assert_fits,
    screen,
    serve,
)
from test_forward_registration_browser import browser as browser  # noqa: E402
from test_postgres_runtime import engine as engine  # noqa: E402

from finance_quant.persistence.forward import SHANGHAI, registration_row_confirmed  # noqa: E402
from finance_quant.persistence.maintenance import write_gate  # noqa: E402
from finance_quant.persistence.schema import dataset, forward_attempt, forward_registration, research_task  # noqa: E402

NEW = REGISTRATIONS + "/new"
SOURCES = "**" + API + "/sources?*"
LONG_NAME = "managed-momentum-universe-" * 6


def form(page):
    return page.get_by_role("button", name="确认登记", exact=True)


def fill(page, name, signal, *, pool="000001.sz，600000.SH\n600001.SH", window=2):
    page.get_by_label("登记名称").fill(name)
    page.get_by_label("信号日", exact=True).fill(signal.isoformat())
    page.get_by_label("固定股票池", exact=True).fill(pool)
    page.get_by_label("动量窗口（交易日）").fill(str(window))


def open_source(page, origin, *, name="fixed-source"):
    page.goto(origin + NEW)
    option = page.get_by_role("radio", name=name)
    expect(option).to_be_enabled()
    option.check()


def open_detail(page, origin, receipt, name, sessions, dataset_id):
    page.wait_for_url(origin + f"{REGISTRATIONS}/{receipt['registration_id']}")
    title = page.get_by_role("heading", name=name, exact=True)
    expect(title).to_be_focused()
    expect(page.get_by_text("固定股票池", exact=True)).to_be_visible()
    expect(page.get_by_text("000001.SZ、600000.SH、600001.SH", exact=True)).to_be_visible()
    expect(page.get_by_text("时间均为北京时间", exact=True).first).to_be_visible()
    for day in sessions[-3:]:
        expect(page.get_by_text(day.isoformat(), exact=True)).to_be_visible()
    expect(page.get_by_text(dataset_id, exact=True)).to_be_visible()
    expect(page.locator(".forward-outcome .ant-tag")).to_have_text("待评分")
    audit = page.locator("summary", has_text="审计信息")
    audit.click()
    confirmed = datetime.fromisoformat(receipt["confirmed_at"]).astimezone(SHANGHAI)
    expect(page.get_by_text(confirmed.strftime("%Y-%m-%d %H:%M:%S"), exact=True)).to_be_visible()
    audit.click()


@pytest.mark.parametrize("width", (1440, 1366, 1024, 390, 320))
def test_create_form_keyboard_or_touch(engine, tmp_path, browser, setup_registration, width):
    _, document, signal, sessions, calendar, source, _ = setup_registration
    start, end = (day.isoformat() for day in (sessions[0], signal))
    chosen, _ = source(calendar, name="leaf-a", manifest_name=LONG_NAME, coverage=(start, end))
    other, _ = source(calendar, name="leaf-b", manifest_name=LONG_NAME, coverage=(start, sessions[-1].isoformat()))
    mobile = width <= 390
    with (
        serve(engine, tmp_path) as origin,
        browser.new_context(
            viewport={"width": width, "height": 840},
            is_mobile=mobile,
            has_touch=mobile,
            timezone_id="America/Los_Angeles",
            reduced_motion="reduce",
        ) as context,
    ):
        page = context.new_page()
        errors, sent = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def track(route):
            sent.append(route.request.post_data_json)
            # Dispatch two more submit events while the first request has no response.
            page.locator("form").evaluate("(form) => { form.requestSubmit(); form.requestSubmit(); }")
            route.continue_()

        page.route("**" + API, track)
        page.goto(origin + REGISTRATIONS)
        link = page.get_by_role("link", name="新建登记")
        activate(page, link, mobile)
        expect(page.get_by_role("heading", name="新建前瞻登记")).to_be_focused()
        expect(page.get_by_role("radio", name=LONG_NAME)).to_have_count(2)
        option = page.get_by_role("radio", name=chosen[-8:])
        other_option = page.get_by_role("radio", name=other[-8:])
        expect(other_option).to_be_visible()
        name = "五档登记验收 " + str(width)
        expect(page.get_by_text("须在信号日北京时间 00:00 前完成确认。", exact=False)).to_be_visible()
        box = page.locator("li").filter(has=option)
        if mobile:
            box.locator("label").tap()
        else:
            option.focus()
            page.keyboard.press("Space")
        expect(option).to_be_checked()
        box.get_by_text("完整标识").click()
        expect(box.get_by_text(chosen, exact=True)).to_be_visible()
        expect(box.get_by_text(f"行情 {start} 至 {end}", exact=False)).to_be_visible()
        assert_fits(page)
        box.get_by_text("完整标识").click()
        fill(page, name, signal)
        assert_fits(page)
        screen(page, f"form-{width}")

        with page.expect_response(
            lambda response: response.url.endswith(API) and response.request.method == "POST"
        ) as result:
            if mobile:
                assert form(page).bounding_box()["height"] >= 44
                form(page).tap()
            else:
                form(page).focus()
                page.keyboard.press("Shift+Tab")
                page.keyboard.press("Tab")
                expect(form(page)).to_be_focused()
                assert form(page).evaluate("el => getComputedStyle(el).outlineStyle") != "none"
                page.keyboard.press("Enter")
        assert result.value.status == 200
        receipt = result.value.json()
        open_detail(page, origin, receipt, name, sessions, chosen)
        assert_fits(page)
        screen(page, f"receipt-{width}")
        assert [data["dataset_id"] for data in sent] == [chosen]
        assert errors == []

    (row,) = _rows(engine, forward_registration)
    assert row["id"] == receipt["registration_id"] and registration_row_confirmed(row)
    assert row["dataset_id"] != document["dataset_id"]
    assert _rows(engine, forward_attempt) == _rows(engine, research_task) == []


def test_create_form_sources_loading_empty_and_selection_refresh(engine, tmp_path, browser, setup_registration):
    _, document, signal, _, _, _, _ = setup_registration
    with engine.begin() as connection:
        connection.execute(update(dataset).values(status="failed"))
    with serve(engine, tmp_path) as origin, browser.new_context(viewport={"width": 320, "height": 840}) as context:
        page = context.new_page()

        def fail_after_loading(route):
            expect(page.get_by_text("正在加载受管来源")).to_be_visible()
            expect(form(page)).to_be_disabled()
            route.fulfill(status=503, json={"detail": "来源测试暂不可读"})

        page.route(SOURCES, fail_after_loading, times=1)
        page.goto(origin + NEW)
        expect(page.get_by_text("受管来源读取失败。来源测试暂不可读")).to_be_visible()
        expect(form(page)).to_be_disabled()
        page.get_by_role("button", name="重试加载来源").click()
        expect(page.get_by_text("暂无可用受管来源", exact=True)).to_be_visible()
        expect(form(page)).to_be_disabled()
        screen(page, "sources-empty-320")
        with engine.begin() as connection:
            connection.execute(update(dataset).values(status="ready"))
        page.get_by_role("button", name="刷新来源").click()
        page.get_by_role("radio", name="fixed-source").check()
        fill(page, "保留的登记", signal)
        with engine.begin() as connection:
            connection.execute(update(dataset).where(dataset.c.id == document["dataset_id"]).values(status="failed"))
        page.get_by_role("button", name="刷新来源").click()
        expect(page.get_by_text("所选来源已不可用，请重新选择。")).to_be_visible()
        expect(form(page)).to_be_disabled()
        expect(page.get_by_label("登记名称")).to_have_value("保留的登记")
        expect(page.get_by_label("信号日", exact=True)).to_have_value(signal.isoformat())
        assert_fits(page)
    assert _rows(engine, forward_registration) == []


def test_create_form_local_validation(engine, tmp_path, browser, setup_registration):
    _, _, signal, _, _, _, _ = setup_registration
    with serve(engine, tmp_path) as origin, browser.new_context(viewport={"width": 320, "height": 840}) as context:
        page = context.new_page()
        seen = []
        page.on("request", lambda request: seen.append(request.url) if request.method == "POST" else None)
        open_source(page, origin)
        fill(page, "边界", signal, pool="000001.SZ、000001.sz、bad", window=253)
        page.get_by_label("前 K 只股票").fill("1.5")
        form(page).click()
        expect(page.get_by_role("alert", name="")).to_contain_text("请检查表单中的字段")
        expect(page.get_by_text("1 只有效股票，1 项重复，1 项格式无效")).to_be_visible()
        for label in ("固定股票池", "动量窗口（交易日）", "前 K 只股票"):
            expect(page.get_by_label(label, exact=True)).to_have_attribute("aria-invalid", "true")
        page.get_by_role("button", name="移除 000001.SZ（第 2 项）").click()
        expect(page.get_by_label("固定股票池", exact=True)).to_be_focused()
        expect(page.get_by_text("1 只有效股票，1 项格式无效")).to_be_visible()
        page.get_by_label("固定股票池", exact=True).fill(" ".join(f"{code:06d}.SH" for code in range(51)))
        expect(page.get_by_text("51 只有效股票")).to_be_visible()
        expect(page.get_by_label("固定股票池", exact=True)).to_have_attribute("aria-invalid", "true")
        assert_fits(page)
        assert seen == []


@pytest.mark.parametrize("case", ("closed", "warmup", "exit", "expired", "stale", "maintenance", "conflict"))
def test_create_form_shows_real_rejections(engine, tmp_path, browser, setup_registration, case):
    client, document, signal, sessions, calendar, source, _ = setup_registration
    market_start, market_end = sessions[0].isoformat(), sessions[-1].isoformat()
    if case in ("warmup", "exit"):
        source(calendar, name="known-coverage", coverage=(market_start, market_end))
    selected_day, window, explanation = {
        "closed": (signal + timedelta(days=1), 2, "信号日必须是受管交易日历中的交易日"),
        "warmup": (signal, 4, "受管交易日历缺少动量预热交易日"),
        "exit": (sessions[-2], 2, "受管交易日历未覆盖入场日与退出日"),
        "expired": (sessions[-4], 2, "00:00"),
        "stale": (signal, 2, "来源数据集不存在或尚未就绪"),
        "maintenance": (signal, 2, "数据维护期间暂停写入"),
        "conflict": (signal, 2, "参数已改变"),
    }[case]
    with serve(engine, tmp_path) as origin, browser.new_context(viewport={"width": 320, "height": 840}) as context:
        page = context.new_page()
        keys = []
        page.on(
            "request",
            lambda request: keys.append(request.header_value("idempotency-key")) if request.method == "POST" else None,
        )
        if case in ("warmup", "exit"):
            open_source(page, origin, name="known-coverage")
            expect(page.get_by_text(f"行情 {market_start} 至 {market_end}", exact=False)).to_be_visible()
        else:
            open_source(page, origin)
        fill(page, "服务端拒绝", selected_day, window=window)
        if case == "stale":
            with engine.begin() as connection:
                connection.execute(update(dataset).values(status="failed"))
        if case == "conflict":
            assert client.post(API, json=document, headers={"Idempotency-Key": "browser-conflict"}).status_code == 200

            def conflict(route):
                route.continue_(headers={**route.request.headers, "idempotency-key": "browser-conflict"})

            page.route("**" + API, conflict, times=1)
        if case == "maintenance":
            with write_gate(engine, exclusive=True):
                form(page).click()
                expect(page.locator(".forward-create-feedback")).to_contain_text(explanation)
        else:
            form(page).click()
            expect(page.locator(".forward-create-feedback")).to_contain_text(explanation)
        expect(page.locator(".forward-create-feedback")).to_be_focused()
        expect(page.get_by_label("登记名称")).to_have_value("服务端拒绝")
        expect(page.get_by_label("信号日", exact=True)).to_be_enabled()
        assert_fits(page)
        screen(page, f"rejected-{case}-320")
        if case == "maintenance":
            page.get_by_label("登记名称").fill("服务端拒绝后修改")
            with write_gate(engine, exclusive=True):
                form(page).click()
                expect(page.locator(".forward-create-feedback")).to_contain_text(explanation)
            assert len(keys) == len(set(keys)) == 2 and all(keys)
    rows = _rows(engine, forward_registration)
    if case == "expired":
        (row,) = rows
        assert row["status"] == "expired" and not registration_row_confirmed(row)
    elif case == "conflict":
        (row,) = rows
        assert registration_row_confirmed(row) and row["name"] == document["name"].strip()
    else:
        assert rows == []
    assert _rows(engine, forward_attempt) == _rows(engine, research_task) == []


@pytest.mark.parametrize(
    "first", ("loss-after-seal", "timeout-before-receipt", "unavailable", "invalid-receipt", "offline")
)
def test_create_form_retries_exact_request(engine, tmp_path, browser, setup_registration, first):
    _, document, signal, sessions, _, _, _ = setup_registration
    with serve(engine, tmp_path) as origin, browser.new_context(viewport={"width": 390, "height": 840}) as context:
        page = context.new_page()
        page.clock.install()
        sent, receipts = [], []

        def deliver(route):
            sent.append((route.request.header_value("idempotency-key"), route.request.post_data))
            if len(sent) > 1:
                response = route.fetch()
                receipts.append(response.json())
                route.fulfill(response=response)
            elif first == "loss-after-seal":
                response = route.fetch()
                assert response.ok
                receipts.append(response.json())
            elif first == "unavailable":
                route.fulfill(status=503, json={"detail": "前瞻登记服务暂不可用"})
            elif first == "invalid-receipt":
                route.fulfill(status=200, json={"registration_id": "fwd-" + "a" * 32, "confirmed_at": "unknown"})
            elif first == "offline":
                route.abort("internetdisconnected")

        page.route("**" + API, deliver)
        open_source(page, origin)
        fill(page, "原参数重试", signal)
        form(page).click()
        if first in ("loss-after-seal", "timeout-before-receipt"):
            expect(page.locator(".forward-create-feedback")).to_contain_text("正在确认登记")
            assert len(sent) == 1
            page.clock.fast_forward(31_000)
        expect(page.locator(".forward-create-feedback")).to_contain_text("尚未确认登记是否成功")
        expect(page.locator(".forward-create-feedback")).to_be_focused()
        expect(page.get_by_label("登记名称")).to_have_value("原参数重试")
        expect(page.get_by_label("登记名称")).to_be_disabled()
        assert len(sent) == 1
        if first == "loss-after-seal":
            (saved,) = _rows(engine, forward_registration)
            assert registration_row_confirmed(saved)
            with engine.begin() as connection:
                connection.execute(update(dataset).values(status="failed"))
            page.get_by_role("button", name="刷新来源").click()
            expect(page.get_by_text("原受管来源已不在当前选项中", exact=False)).to_be_visible()
        else:
            assert _rows(engine, forward_registration) == []
        screen(page, f"unconfirmed-{first}-390")
        page.get_by_role("button", name="按原参数重试").click()
        page.wait_for_url("**/forward-registrations/fwd-*")
        open_detail(page, origin, receipts[-1], "原参数重试", sessions, document["dataset_id"])
        assert sent[0] == sent[1] and sent[0][0]
        assert len(set(receipt["registration_id"] for receipt in receipts)) == 1
    (saved,) = _rows(engine, forward_registration)
    assert registration_row_confirmed(saved) and saved["id"] == receipts[-1]["registration_id"]
    assert _rows(engine, forward_attempt) == _rows(engine, research_task) == []
