"""Exercise manual score and maturity actions against the built UI and isolated PostgreSQL."""

from __future__ import annotations

import os
import re
import sys
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
from playwright.sync_api import expect
from sqlalchemy import event

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "tests" / "integration")]

from test_forward_actions_api_postgres import _accepted, _client, _post, _queue_rows  # noqa: E402
from test_forward_registration_browser import API, REGISTRATIONS, activate, assert_fits, expect_restored, screen, serve  # noqa: E402
from test_forward_registration_browser import browser as browser  # noqa: E402
from test_forward_score_capture_postgres import setup_capture as setup_capture  # noqa: E402
from test_forward_score_postgres import _execute  # noqa: E402
from test_postgres_runtime import engine as engine  # noqa: E402

from finance_quant.application.forward_label_capture import publish_forward_label_snapshot  # noqa: E402
from finance_quant.application.forward_score_capture import publish_forward_score_snapshot  # noqa: E402
from finance_quant.application.research_requests import lookup_snapshot_meta  # noqa: E402
from finance_quant.persistence.forward import registration_evidence_digest  # noqa: E402
from finance_quant.persistence.maintenance import write_gate  # noqa: E402
from finance_quant.persistence.schema import forward_registration  # noqa: E402
from finance_quant.persistence.tasks import PgTaskStore  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("FINANCE_QUANT_TEST_POSTGRES") != "1", reason="requires isolated PG")


@contextmanager
def mature(engine, row, supplier):
    as_of = datetime.fromisoformat(row["data_rules"]["label_capture_after"])
    supplier.sessions = pd.DatetimeIndex([row[f"{part}_date"] for part in ("signal", "entry", "exit")])

    def clock(connection, cursor, statement, parameters, context, executemany):
        if statement.lower().startswith("select clock_timestamp()"):
            return "select cast(%s as timestamptz)", (as_of.isoformat(),)
        return statement, parameters

    event.listen(engine, "before_cursor_execute", clock, retval=True)
    try:
        yield as_of
    finally:
        event.remove(engine, "before_cursor_execute", clock)


def score_panel(page):
    return page.get_by_role("region", name="评分", exact=True)


def verify_panel(page):
    return page.get_by_role("region", name="到期验证", exact=True)


def submit(panel, phase):
    return panel.get_by_role("button", name="提交评分" if phase == "score" else "提交到期验证", exact=True)


def refresh(page):
    page.get_by_role("button", name="刷新登记", exact=True).click()


def assert_feedback_focus(page, feedback):
    expect(feedback).to_be_focused()
    expect(feedback).to_be_in_viewport(ratio=1)
    assert feedback.bounding_box()["y"] >= page.locator(".app-header").bounding_box()["height"]


def assert_busy(button):
    expect(button).to_have_attribute("aria-busy", "true")
    icon = button.locator(".anticon-loading")
    expect(icon).to_be_visible()
    expect(icon).to_have_attribute("aria-hidden", "true")
    expect(button.get_by_role("img", name="loading")).to_have_count(0)


def post_receipt(page, url, button, mobile):
    expect(button).to_be_enabled()
    with page.expect_response(lambda response: response.url == url and response.request.method == "POST") as posted:
        activate(page, button, mobile)
    return posted.value


def visit_task(page, link, origin, detail_path, row, result, stage, mobile):
    activate(page, link, mobile)
    page.wait_for_url(origin + "/tasks?focus=" + result["task_id"])
    expect(page.get_by_text(stage, exact=True).first).to_be_visible()
    expect(page.get_by_text(row["id"], exact=True).first).to_be_visible()
    page.go_back()
    page.wait_for_url(origin + detail_path)
    accept = page.locator("#forward-receipt-" + result["attempt_id"])
    expect_restored(page, accept)


def deny_detail_once(page, api_path, *, busy_button=None):
    def deny(route):
        assert route.request.method == "GET"
        try:
            if busy_button is not None:
                assert_busy(busy_button)
        finally:
            route.fulfill(status=403, json={"detail": "private access error"})

    page.route("**" + api_path, deny, times=1)


def assert_detail_hidden(page, row):
    expect(page.get_by_text("详情不可用", exact=True)).to_be_visible()
    expect(page.get_by_text("private access error", exact=True)).to_have_count(0)
    expect(page.get_by_role("heading", name=row["request"]["name"], exact=True)).to_have_count(0)
    expect(page.get_by_text("冻结参数", exact=True)).to_have_count(0)
    expect(page.locator(".forward-action-feedback")).to_have_count(0)
    expect(page.locator('a[href*="/tasks?focus="]')).to_have_count(0)


@pytest.mark.parametrize("width", (1440, 1366, 1024, 390, 320))
def test_manual_actions_worker_and_return_focus(engine, tmp_path, browser, setup_capture, width):
    root, row, supplier, publish = setup_capture
    first = publish()
    second = publish_forward_score_snapshot(
        engine, root, row["id"], storage_root=tmp_path, provider=supplier, key="browser-second-option"
    )
    store = PgTaskStore(engine=engine)
    path = REGISTRATIONS + "/" + row["id"]
    api_path = API + "/" + row["id"]
    mobile = width <= 390
    with (
        serve(engine, tmp_path, data_root=root) as origin,
        browser.new_context(
            viewport={"width": width, "height": {320: 568, 390: 740}.get(width, 840)},
            is_mobile=mobile,
            has_touch=mobile,
            timezone_id="America/Los_Angeles",
            reduced_motion="reduce",
        ) as context,
    ):
        page = context.new_page()
        seen = []
        score, verify = score_panel(page), verify_panel(page)

        def detail_read(response):
            return response.url == origin + api_path and response.request.method == "GET"

        def score_once(route):
            seen.append(route.request.post_data_json)
            try:
                assert_busy(submit(score, "score"))
                # Duplicate DOM clicks while the first request has no response.
                page.evaluate("""() => {
                  const button = document.querySelector('[aria-labelledby="forward-action-score"] button.ant-btn-primary');
                  button?.click();
                  button?.click();
                }""")
            finally:
                route.continue_()

        def inspect_verify_submission(route):
            try:
                assert_busy(submit(verify, "verify"))
            finally:
                route.continue_()

        page.route("**" + api_path + "/score", score_once)
        page.route("**" + api_path + "/verify", inspect_verify_submission)
        page.goto(origin + path)
        # With transitions suppressed, the default spinner can miss its leave event.
        page.add_style_tag(
            content="""
            .ant-btn .ant-btn-loading-icon-motion-appear,
            .ant-btn .ant-btn-loading-icon-motion-enter,
            .ant-btn .ant-btn-loading-icon-motion-leave {
                transition: none !important;
                animation: none !important;
            }
            """
        )
        selection = score.get_by_role("combobox", name="评分快照", exact=True)
        expect(selection.locator("option")).to_have_count(3)
        assert set(selection.locator("option").evaluate_all("(options) => options.map(option => option.value)")) == {
            "",
            first["id"],
            second["id"],
        }
        expect(submit(score, "score")).to_be_disabled()
        expect(submit(verify, "verify")).to_have_count(0)
        if mobile:
            selection.select_option(first["id"])
        else:
            selection.focus()
            page.keyboard.press("ArrowDown")
            if selection.input_value() != first["id"]:
                page.keyboard.press("ArrowDown")
        expect(selection).to_have_value(first["id"])
        expect(score.get_by_text(first["id"], exact=True)).to_be_visible()
        assert_fits(page)
        screen(page, f"actions-options-{width}")

        posted = post_receipt(page, origin + api_path + "/score", submit(score, "score"), mobile)
        assert posted.status == 202
        received = posted.json()
        feedback = score.locator(".forward-action-feedback")
        assert_feedback_focus(page, feedback)
        expect(feedback).to_contain_text("评分已入队")
        expect(feedback).to_contain_text(received["attempt_id"])
        assert seen == [{"score_snapshot_id": first["id"]}]
        assert len(_queue_rows(engine)[0]) == len(_queue_rows(engine)[1]) == 1
        assert_fits(page)
        screen(page, f"actions-score-queued-{width}")
        visit_task(
            page, feedback.get_by_role("link", name="查看评分任务"), origin, path, row, received, "前瞻评分", mobile
        )

        assert _execute(store, root, received)[1]["status"] == "succeeded"
        refresh(page)
        expect(verify.get_by_text("等待退出日。", exact=True)).to_be_visible()
        expect(submit(verify, "verify")).to_have_count(0)
        expect(verify.get_by_text(f"最早验证 {row['exit_date'].isoformat()} 15:00:00（北京时间）")).to_be_visible()

        if width == 1440:
            # Let the server clock disagree with the page's last successful read.
            with mature(engine, row, supplier):
                refresh(page)
                expect(submit(verify, "verify")).to_be_enabled()
            before = _queue_rows(engine)
            early = post_receipt(page, origin + api_path + "/verify", submit(verify, "verify"), mobile)
            assert early.status == 409 and early.json()["detail"] == "before-verification-maturity"
            rejection = verify.locator(".forward-action-feedback")
            assert_feedback_focus(page, rejection)
            expect(rejection).to_contain_text("尚未到期")
            expect(rejection).to_contain_text(f"最早验证 {row['exit_date'].isoformat()} 15:00:00（北京时间）")
            assert _queue_rows(engine) == before

        with mature(engine, row, supplier):
            refresh(page)
            expect(submit(verify, "verify")).to_be_enabled()
            before = _queue_rows(engine)
            with page.expect_response(detail_read) as waiting_read:
                waiting = post_receipt(page, origin + api_path + "/verify", submit(verify, "verify"), mobile)
            assert waiting.status == 200 and waiting.json() == {"status": "awaiting_quote"}
            assert waiting_read.value.ok
            waiting_detail = waiting_read.value.json()
            assert waiting_detail["available_actions"]["verify"], waiting_detail["state"]
            assert waiting.request.post_data_json == {}
            quote = verify.locator(".forward-action-feedback")
            assert_feedback_focus(page, quote)
            expect(quote).to_contain_text("本次未找到合格标签")
            expect(submit(verify, "verify")).to_have_attribute("aria-busy", "false")
            expect(quote.get_by_role("link")).to_have_count(0)
            assert _queue_rows(engine) == before
            assert_fits(page)
            screen(page, f"actions-awaiting-quote-{width}")

            label = publish_forward_label_snapshot(
                engine, root, row["id"], storage_root=tmp_path, provider=supplier, key="browser-mature-label"
            )
            with page.expect_response(detail_read) as queued_read:
                posted = post_receipt(page, origin + api_path + "/verify", submit(verify, "verify"), mobile)
            assert posted.status == 202
            result = posted.json()
            assert queued_read.value.ok
            assert result["attempt_id"] in {attempt["id"] for attempt in queued_read.value.json()["attempts"]}
            assert_feedback_focus(page, quote)
            expect(quote).to_contain_text("验证已入队")
            assert len(_queue_rows(engine)[0]) == len(_queue_rows(engine)[1]) == 2
            assert_fits(page)
            screen(page, f"actions-verify-queued-{width}")
            refresh_button = page.get_by_role("button", name="刷新登记", exact=True)
            deny_detail_once(page, api_path, busy_button=refresh_button)
            if mobile:
                refresh_button.tap()
            else:
                activate(page, refresh_button, mobile=False)
            assert_detail_hidden(page, row)

            page.route(
                "**" + api_path, lambda route: route.fulfill(status=503, json={"detail": "详情暂不可读"}), times=1
            )
            retry_read = page.get_by_role("button", name="重试", exact=True)
            for status in (503, 200):
                with page.expect_response(detail_read) as fetched:
                    if mobile:
                        retry_read.tap()
                    else:
                        activate(page, retry_read, mobile=False)
                assert fetched.value.status == status
                if status == 503:
                    expect(page.get_by_text("详情加载失败", exact=True)).to_be_visible()
                    expect(quote).to_have_count(0)
                    expect(page.locator('a[href*="/tasks?focus="]')).to_have_count(0)

            verify_link = quote.get_by_role("link", name="查看验证任务")
            expect(verify_link).to_have_attribute("href", "/tasks?focus=" + result["task_id"])
            # Wait for any deferred history focus restoration before checking the final focus.
            page.evaluate("() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))")
            assert_feedback_focus(page, quote)
            expect(verify_link).to_be_in_viewport(ratio=1)
            screen(page, f"actions-verify-recovered-after-score-return-{width}", full_page=False)
            visit_task(page, verify_link, origin, path, row, result, "到期验证", mobile)
            assert _execute(store, root, result)[1]["status"] == "succeeded"
            with page.expect_response(detail_read) as completed_read:
                refresh(page)
            assert completed_read.value.ok
            expect(refresh_button).to_have_attribute("aria-busy", "false")
            expect(page.get_by_text("2.33 个百分点", exact=True)).to_be_visible()
            expect(verify.get_by_text(label["id"], exact=True)).to_be_visible()
            assert_fits(page)


@pytest.mark.parametrize("phase", ("score", "verify"))
@pytest.mark.parametrize("delivered", (False, True))
def test_manual_actions_unconfirmed_replay_during_maintenance(
    engine, tmp_path, browser, setup_capture, monkeypatch, phase, delivered
):
    root, row, supplier, publish = setup_capture
    score_version = publish()["id"]
    path = REGISTRATIONS + "/" + row["id"]
    api_path = API + "/" + row["id"]
    if phase == "verify":
        client = _client(engine, root, tmp_path, monkeypatch)
        accepted = _accepted(
            _post(client, row["id"], "score", {"score_snapshot_id": score_version}, "browser-score-first")
        )
        assert _execute(PgTaskStore(engine=engine), root, accepted)[1]["status"] == "succeeded"
    with mature(engine, row, supplier) if phase == "verify" else nullcontext():
        if phase == "verify":
            publish_forward_label_snapshot(
                engine, root, row["id"], storage_root=tmp_path, provider=supplier, key="browser-label-first"
            )
        with (
            serve(engine, tmp_path, data_root=root) as origin,
            browser.new_context(viewport={"width": 320, "height": 568}) as context,
        ):
            page = context.new_page()
            page.clock.install()
            panel = score_panel(page) if phase == "score" else verify_panel(page)
            endpoint = api_path + "/" + phase
            sent, receipts = [], []

            def pending(route):
                sent.append((route.request.header_value("idempotency-key"), route.request.post_data))
                if len(sent) == 1 and not delivered:
                    return
                response = route.fetch()
                if response.status == 202:
                    receipts.append(response.json())
                if len(sent) > 1:
                    route.fulfill(response=response)
                else:
                    assert response.status == 202
                    route.abort("connectionreset")

            page.route("**" + endpoint, pending)
            page.goto(origin + path)
            if phase == "score":
                panel.get_by_role("combobox", name="评分快照", exact=True).select_option(score_version)
            before = _queue_rows(engine)
            submit(panel, phase).click()
            feedback = panel.locator(".forward-action-feedback")
            if not delivered:
                expect(feedback).to_contain_text("正在提交")
                page.clock.fast_forward(31_000)
            expect(feedback).to_contain_text("尚未确认是否已入队")
            assert len(sent) == 1
            assert_feedback_focus(page, feedback)
            assert len(_queue_rows(engine)[0]) == len(before[0]) + int(delivered)
            retry = feedback.get_by_role("button", name="按原参数重试")
            if delivered:
                deny_detail_once(page, api_path)
                refresh(page)
                assert_detail_hidden(page, row)
                screen(page, f"actions-{phase}-403", full_page=False)
                with page.expect_response(lambda response: response.url == origin + api_path) as recovered:
                    page.get_by_role("button", name=re.compile(r"^重\s*试$")).click()
                assert recovered.value.status == 200
                expect(feedback).to_contain_text("尚未确认是否已入队")
                assert_feedback_focus(page, feedback)
                page.keyboard.press("Tab")
                expect(retry).to_be_focused()
                screen(page, f"actions-{phase}-403-recovered", full_page=False)
                expect(panel.get_by_text("尝试已排队或正在运行", exact=False)).to_be_visible()
                if phase == "score":
                    expect(feedback.get_by_text(score_version, exact=True)).to_be_visible()
                    relative, _, _ = lookup_snapshot_meta(root, score_version, allow_score=True)
                    (root / relative / "manifest.json").unlink()
            else:
                refresh(page)
            expect(retry).to_be_enabled()
            with write_gate(engine, exclusive=True):
                with page.expect_response(
                    lambda response: response.url.endswith(endpoint) and response.request.method == "POST"
                ) as blocked:
                    retry.click()
                assert blocked.value.status == 409
                expect(feedback).to_contain_text("数据维护期间暂停写入")
                expect(feedback).to_contain_text("尚未确认是否已入队")
                expect(retry).to_be_enabled()
                assert_feedback_focus(page, feedback)
            screen(page, f"actions-{phase}-maintenance-{delivered}", full_page=False)
            retry.click()
            expect(feedback).to_contain_text("已入队")
            assert len(sent) == 3 and sent[0][0] and all(item == sent[0] for item in sent)
            assert all(item == receipts[0] for item in receipts)
            assert len(_queue_rows(engine)[0]) == len(_queue_rows(engine)[1]) == len(before[0]) + 1
            expect(feedback.get_by_role("link")).to_have_attribute("href", "/tasks?focus=" + receipts[0]["task_id"])


def test_score_rejection_refresh_failure_and_bound_retry(engine, tmp_path, browser, setup_capture):
    root, row, _, publish = setup_capture
    store = PgTaskStore(engine=engine)
    path = REGISTRATIONS + "/" + row["id"]
    api_path = API + "/" + row["id"]
    with (
        serve(engine, tmp_path, data_root=root) as origin,
        browser.new_context(viewport={"width": 390, "height": 740}) as context,
    ):
        page = context.new_page()
        sent = []
        page.on(
            "request",
            lambda request: (
                sent.append((request.header_value("idempotency-key"), request.post_data))
                if request.method == "POST"
                else None
            ),
        )
        score = score_panel(page)
        feedback = score.locator(".forward-action-feedback")
        options_url = "**" + api_path + "/score-options"
        page.route(options_url, lambda route: route.fulfill(status=503, json={"detail": "版本暂不可读"}), times=1)
        page.goto(origin + path)
        expect(score.get_by_text("评分快照读取失败。版本暂不可读")).to_be_visible()
        expect(submit(score, "score")).to_be_disabled()
        score.get_by_role("button", name="重试加载评分快照").click()
        expect(score.get_by_text("暂无匹配的已发布评分快照。")).to_be_visible()

        version = publish()["id"]
        relative, _, _ = lookup_snapshot_meta(root, version, allow_score=True)
        manifest = root / relative / "manifest.json"
        manifest_bytes = manifest.read_bytes()
        refresh_options = score.get_by_role("button", name="刷新评分快照")
        refresh_options.click()
        selection = score.get_by_role("combobox", name="评分快照", exact=True)
        selection.select_option(version)
        manifest.unlink()
        refresh_options.click()
        expect(score.get_by_text("所选评分快照已不可用，请重新选择。")).to_be_visible()
        expect(submit(score, "score")).to_be_disabled()
        manifest.write_bytes(manifest_bytes)
        refresh_options.click()
        selection.select_option(version)

        with write_gate(engine, exclusive=True):
            with page.expect_response(
                lambda r: r.url.endswith(api_path + "/score") and r.request.method == "POST"
            ) as blocked:
                submit(score, "score").click()
            assert blocked.value.status == 409
            expect(feedback).to_contain_text("评分提交被拒绝")
            expect(feedback).to_contain_text("数据维护期间暂停写入")
            assert_feedback_focus(page, feedback)
            expect(selection).to_have_value(version)
            assert _queue_rows(engine) == [[], []]

        page.route("**" + api_path, lambda route: route.fulfill(status=503, json={"detail": "详情暂不可读"}), times=1)
        with page.expect_response(
            lambda r: r.url.endswith(api_path + "/score") and r.request.method == "POST"
        ) as submitted:
            submit(score, "score").click()
        first = submitted.value.json()
        expect(page.get_by_text("详情加载失败", exact=True)).to_be_visible()
        expect(feedback).to_contain_text("评分已入队")
        task_href = "/tasks?focus=" + first["task_id"]
        expect(feedback.get_by_role("link")).to_have_attribute("href", task_href)

        deny_detail_once(page, api_path)
        page.get_by_role("button", name=re.compile(r"^重\s*试$")).click()
        assert_detail_hidden(page, row)
        page.route("**" + api_path, lambda route: route.fulfill(status=503, json={"detail": "详情暂不可读"}), times=1)
        page.get_by_role("button", name=re.compile(r"^重\s*试$")).click()
        expect(page.get_by_text("详情加载失败", exact=True)).to_be_visible()
        expect(feedback).to_have_count(0)
        expect(page.locator(f'a[href="{task_href}"]')).to_have_count(0)
        page.get_by_role("button", name=re.compile(r"^重\s*试$")).click()
        expect(feedback).to_contain_text(first["attempt_id"])
        expect(feedback.get_by_role("link")).to_have_attribute("href", task_href)
        assert_feedback_focus(page, feedback)
        screen(page, "actions-receipt-recovered-390", full_page=False)

        # Keep the same SPA mounted while changing the registration route.
        other = dict(row, id="fwd-other", idempotency_key="browser-other", name="另一条前瞻登记")
        other["request"] = dict(row["request"], name=other["name"])
        other["confirmed_evidence_sha256"] = registration_evidence_digest(other)
        with engine.begin() as connection:
            connection.execute(forward_registration.insert().values(**other))
        page.evaluate(
            """path => {
                history.pushState(null, "", path);
                window.dispatchEvent(new PopStateEvent("popstate"));
            }""",
            REGISTRATIONS + "/" + other["id"],
        )
        expect(page.get_by_role("heading", name="另一条前瞻登记")).to_be_visible()
        expect(page.locator(".forward-action-feedback")).to_have_count(0)
        expect(page.locator(f'a[href="{task_href}"]')).to_have_count(0)
        page.go_back()
        expect(page.get_by_role("heading", name=row["request"]["name"])).to_be_visible()

        manifest.unlink()
        assert _execute(store, root, first)[1]["status"] == "failed"
        manifest.write_bytes(manifest_bytes)
        refresh(page)
        expect(page.get_by_text("评分失败", exact=True).first).to_be_visible()
        expect(score.get_by_text("已绑定评分快照", exact=True)).to_be_visible()
        expect(score.get_by_text(version, exact=True)).to_be_visible()
        expect(selection).to_have_count(0)
        expect(submit(score, "score")).to_be_enabled()

        page.route("**" + api_path + "/score", lambda route: route.fulfill(status=202, json=first), times=1)
        submit(score, "score").click()
        expect(feedback).to_contain_text("尚未确认是否已入队")
        expect(feedback.get_by_role("link")).to_have_count(0)
        assert len(_queue_rows(engine)[0]) == 1
        feedback.get_by_role("button", name="按原参数重试").click()
        expect(feedback).to_contain_text("评分已入队")
        second_link = feedback.get_by_role("link", name="查看评分任务")
        task_id = second_link.get_attribute("href").split("=", 1)[1]
        assert task_id != first["task_id"]
        assert len(set(key for key, _ in sent)) == 3 and sent[-1] == sent[-2]
        assert len(_queue_rows(engine)[0]) == len(_queue_rows(engine)[1]) == 2
        second_link.click()
        page.wait_for_url(origin + "/tasks?focus=" + task_id)
        expect(page.get_by_text("登记标识", exact=True)).to_be_visible()
        expect(page.get_by_text(row["id"], exact=True).first).to_be_visible()
        store.cancel(task_id)
        attempt_id = next(attempt["id"] for attempt in _queue_rows(engine)[1] if attempt["task_id"] == task_id)
        page.go_back()
        expect_restored(page, page.locator("#forward-attempt-" + attempt_id))
