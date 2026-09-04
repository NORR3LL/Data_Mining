from __future__ import annotations

import logging
import json
import re
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

LOGGER = logging.getLogger(__name__)


class Collector:
    def __init__(self, config: dict[str, Any], config_path: Path) -> None:
        self.config = config
        self.root = config_path.resolve().parent
        self.runtime = self.root / "runtime"
        self.output = self.root / config.get("output_dir", "output")
        self.auth_file = self.runtime / "auth_state.json"
        self.extracted_records: list[dict[str, str]] = self._load_extracted_records()

    def run(self, variables: dict[str, str]) -> list[Path]:
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.output.mkdir(parents=True, exist_ok=True)
        browser_config = self.config.get("browser", {})
        timeout_ms = int(browser_config.get("timeout_seconds", 30)) * 1000
        results: list[Path] = []
        manifest: list[dict[str, str]] = []

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=bool(browser_config.get("headless", False)),
                slow_mo=int(browser_config.get("slow_mo_ms", 0)),
            )
            context_args: dict[str, Any] = {"accept_downloads": True}
            if self.auth_file.exists():
                context_args["storage_state"] = str(self.auth_file)
            context = browser.new_context(**context_args)
            context.set_default_timeout(timeout_ms)
            page = context.new_page()

            try:
                self._ensure_login(context, page)
                for action in self.config["site"].get("post_login_actions", []):
                    self._action(page, action, variables)
                for task in self.config["tasks"]:
                    if not task.get("enabled", True):
                        continue
                    try:
                        result = self._run_task(page, task, variables)
                        results.append(result)
                        manifest.append({"task": task["name"], "status": "success", "file": str(result)})
                    except Exception as exc:
                        screenshot = self._failure_screenshot(page, task["name"], variables)
                        manifest.append(
                            {"task": task["name"], "status": "failed", "error": str(exc), "screenshot": str(screenshot)}
                        )
                        LOGGER.exception("任务失败：%s", task["name"])
                        if not task.get("continue_on_error", True):
                            raise
                context.storage_state(path=str(self.auth_file))
                self._write_extracted_records()
                if browser_config.get("pause_after_run", False):
                    input("RPA 流程已执行，请检查浏览器页面，确认后按 Enter 关闭：")
            finally:
                context.close()
                browser.close()
        self._write_manifest(manifest, variables)
        return results

    def _ensure_login(self, context: BrowserContext, page: Page) -> None:
        site = self.config["site"]
        page.goto(site["login_url"], wait_until="domcontentloaded")
        if self._logged_in(page):
            LOGGER.info("已复用登录状态")
            return

        timeout_ms = int(site.get("login_timeout_seconds", 300)) * 1000
        LOGGER.info("请在弹出的浏览器中完成登录，程序正在等待登录成功……")
        if site.get("manual_login_confirmation", False):
            input("请在浏览器中完成淘宝星河登录，进入工作台后回到此窗口按 Enter：")
            context.storage_state(path=str(self.auth_file))
            LOGGER.info("登录状态已保存")
            return
        try:
            selector = site.get("login_success_selector")
            url_part = site.get("login_success_url_contains")
            if selector:
                page.locator(selector).wait_for(state="visible", timeout=timeout_ms)
            elif url_part:
                page.wait_for_url(re.compile(f".*{re.escape(url_part)}.*"), timeout=timeout_ms)
            else:
                raise ValueError("必须配置 login_success_selector 或 login_success_url_contains")
        except PlaywrightTimeout as exc:
            raise RuntimeError("等待登录超时，请重新运行后登录") from exc
        context.storage_state(path=str(self.auth_file))

    def _logged_in(self, page: Page) -> bool:
        site = self.config["site"]
        selector = site.get("login_success_selector")
        url_part = site.get("login_success_url_contains")
        if selector:
            try:
                if page.locator(selector).first.is_visible(timeout=2000):
                    return True
            except PlaywrightTimeout:
                pass
        return bool(url_part and url_part in page.url)

    def _run_task(self, page: Page, task: dict[str, Any], variables: dict[str, str]) -> Path:
        name = task["name"]
        LOGGER.info("开始任务：%s", name)
        page.goto(self._format(task["page_url"], variables), wait_until="domcontentloaded")
        if not self._logged_in(page) and "login" in page.url.lower():
            raise RuntimeError(f"任务“{name}”执行前登录状态已失效，请重新运行")

        for action in task.get("actions", []):
            self._action(page, action, variables)

        result = task["result"]
        result_type = result.get("type", "download")
        filename = self._format(result["filename"], variables)
        destination = self.output / self._safe_filename(filename)

        if result_type == "download":
            with page.expect_download() as download_info:
                page.locator(result["selector"]).click()
            download_info.value.save_as(destination)
        else:
            raise ValueError(f"第一阶段仅支持 download，任务“{name}”配置为：{result_type}")

        if not destination.exists() or destination.stat().st_size == 0:
            raise RuntimeError(f"任务“{name}”没有生成有效文件")
        LOGGER.info("任务完成：%s -> %s", name, destination)
        return destination

    def _action(self, page: Page, action: dict[str, Any], variables: dict[str, str]) -> None:
        if not action.get("enabled", True):
            return
        action_type = action["type"]
        selector = action.get("selector")
        if action_type == "fill":
            page.locator(selector).fill(self._format(str(action.get("value", "")), variables))
        elif action_type == "click":
            page.locator(selector).click()
        elif action_type == "click_role":
            page.get_by_role(
                str(action["role"]),
                name=self._format(str(action["name"]), variables),
                exact=bool(action.get("exact", True)),
            ).click()
        elif action_type == "click_text":
            expected = self._format(str(action["text"]), variables)
            deadline = time.monotonic() + float(action.get("timeout_seconds", 30))
            while time.monotonic() < deadline:
                for frame in page.frames:
                    target = frame.get_by_text(
                        expected,
                        exact=bool(action.get("exact", True)),
                    )
                    for index in range(target.count()):
                        try:
                            if target.nth(index).is_visible():
                                target.nth(index).click()
                                LOGGER.info("已点击文本：%s（frame: %s）", expected, frame.url)
                                return
                        except PlaywrightTimeout:
                            continue
                page.wait_for_timeout(300)
            frame_urls = [frame.url for frame in page.frames]
            raise RuntimeError(f"所有页面框架中均未找到可见文本“{expected}”：{frame_urls}")
        elif action_type == "select_date_range":
            self._select_date_range(
                page,
                self._format(str(action["start"]), variables),
                self._format(str(action["end"]), variables),
                str(action.get("trigger_text", "至")),
            )
        elif action_type == "fill_date_range_confirm_each":
            actual_start, actual_end = self._fill_date_range_confirm_each(
                page,
                self._format(str(action["start"]), variables),
                self._format(str(action["end"]), variables),
                str(action.get("separator_text", "至")),
            )
            variables["start_date"] = actual_start
            variables["end_date"] = actual_end
        elif action_type == "extract_currency_metric":
            self._extract_currency_metric(
                page,
                label=str(action["label"]),
                field=str(action["field"]),
                variables=variables,
            )
        elif action_type == "setup_july_report":
            self._setup_july_report(page, action, variables)
        elif action_type == "download_content_report":
            self._download_content_report(page, action, variables)
        elif action_type == "open_nested_card_detail":
            self._open_nested_card_detail(page, action, variables)
        elif action_type == "locate_texts":
            for raw_text in action["texts"]:
                expected = self._format(str(raw_text), variables)
                matches = page.get_by_text(expected, exact=bool(action.get("exact", True)))
                matches.first.wait_for(state="visible")
                LOGGER.info("已定位字段：%s（匹配 %s 个）", expected, matches.count())
        elif action_type == "visit_details":
            list_url = page.url
            for raw_text in action["texts"]:
                expected = self._format(str(raw_text), variables)
                variables["project_name"] = expected
                title = page.get_by_text(expected, exact=True).first
                title.wait_for(state="visible")
                card = title.locator(
                    "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' mux-card ')][1]"
                )
                card.hover()
                detail_icon = card.locator(
                    "span[data-spm-click*='projectList_detail']"
                ).first
                detail_icon.wait_for(state="visible")
                detail_page = page
                try:
                    with page.context.expect_page(timeout=5000) as new_page_info:
                        detail_icon.click()
                    detail_page = new_page_info.value
                    detail_page.wait_for_load_state("domcontentloaded")
                    LOGGER.info("详情已在新标签页打开：%s", expected)
                except PlaywrightTimeout:
                    # 没有新标签页时，点击已经在当前页完成。
                    detail_page.wait_for_load_state("domcontentloaded")
                LOGGER.info("已进入详情：%s", expected)
                variables["project_detail_url"] = detail_page.url
                for detail_action in action.get("detail_actions", []):
                    self._action(detail_page, detail_action, variables)
                if action.get("pause_each", False):
                    input(f"已完成“{expected}”详情动作，检查完成后按 Enter 返回列表：")
                if detail_page is not page:
                    detail_page.close()
                else:
                    page.goto(list_url, wait_until="domcontentloaded")
                page.get_by_text(expected, exact=True).first.wait_for(state="visible")
                LOGGER.info("已返回项目列表：%s", expected)
            variables.pop("project_name", None)
            variables.pop("project_detail_url", None)
        elif action_type == "select":
            page.locator(selector).select_option(self._format(str(action["value"]), variables))
        elif action_type == "check":
            page.locator(selector).check()
        elif action_type == "uncheck":
            page.locator(selector).uncheck()
        elif action_type == "press":
            page.locator(selector).press(str(action["value"]))
        elif action_type == "wait":
            page.locator(selector).wait_for(state=action.get("state", "visible"))
        elif action_type == "wait_ms":
            page.wait_for_timeout(int(action.get("value", 1000)))
        else:
            raise ValueError(f"不受支持的动作类型：{action_type}")

    def _failure_screenshot(self, page: Page, task_name: str, variables: dict[str, str]) -> Path:
        directory = self.root / "logs" / "screenshots"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / self._safe_filename(f"{task_name}_{variables['run_time']}.png")
        try:
            page.screenshot(path=str(path), full_page=True)
        except Exception:
            LOGGER.exception("保存失败截图时发生错误")
        return path

    def _setup_july_report(
        self, page: Page, action: dict[str, Any], variables: dict[str, str]
    ) -> None:
        list_url = page.url
        project_name = str(action["project_name"])
        project_title = page.get_by_text(project_name, exact=True).first
        project_title.wait_for(state="visible")
        project_card = project_title.locator(
            "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' mux-card ')][1]"
        )
        project_card.hover()
        project_card.locator("span[data-spm-click*='projectList_detail']").first.click()
        LOGGER.info("已进入特殊项目：%s", project_name)

        child_name = str(action["child_name"])
        child_title = page.get_by_text(child_name, exact=False).first
        child_title.wait_for(state="visible")
        child_card = child_title.locator(
            "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' mux-card ')][1]"
        )
        child_card.hover()
        child_card.get_by_text(str(action.get("detail_text", "查看详情")), exact=False).first.click()
        LOGGER.info("已进入达人卡片详情：%s", child_name)

        self._action(
            page,
            {"type": "click_text", "text": action.get("effect_text", "推广效果"), "exact": False},
            variables,
        )
        self._action(
            page,
            {"type": "click_text", "text": action.get("dimension_text", "内容维度"), "exact": False},
            variables,
        )
        LOGGER.info("已进入内容维度：%s", child_name)

        actual_start, actual_end = self._fill_date_range_confirm_each(
            page,
            self._format(str(action["start"]), variables),
            self._format(str(action["end"]), variables),
            str(action.get("separator_text", "至")),
        )
        variables["start_date"] = actual_start
        variables["end_date"] = actual_end
        detail_heading = page.get_by_text(
            str(action.get("detail_heading", "数据明细")), exact=True
        ).first
        detail_heading.wait_for(state="visible")
        detail_section = detail_heading.locator(
            "xpath=ancestor::*[.//*[contains(normalize-space(.), '下载报表')]][1]"
        )
        attributions = detail_section.get_by_text(
            str(action.get("attribution_text", "归因口径")), exact=False
        )
        attribution_clicked = False
        for index in range(attributions.count()):
            if attributions.nth(index).is_visible():
                attributions.nth(index).click()
                attribution_clicked = True
                break
        if not attribution_clicked:
            raise RuntimeError("数据明细模块中未找到可见的归因口径控件")
        LOGGER.info("已点击数据明细模块内的归因口径")
        self._action(
            page,
            {"type": "click_text", "text": action.get("attribution_value", "30天"), "exact": True},
            variables,
        )
        LOGGER.info("七月项目筛选已完成：%s 至 %s，归因口径 %s", actual_start, actual_end, action.get("attribution_value", "30天"))
        if action.get("pause_before_download", False):
            input("七月项目筛选已设置，按 Enter 开始生成并下载报表：")

        self._click_visible_text_in_scope(
            detail_section, str(action.get("download_report_text", "下载报表"))
        )
        LOGGER.info("已点击下载报表")
        confirm = page.get_by_role(
            "button", name=str(action.get("confirm_text", "确定")), exact=True
        )
        confirm.wait_for(state="visible")
        confirm.click()
        LOGGER.info("已确认生成报表")

        records_text = str(action.get("download_records_text", "下载记录"))
        self._wait_for_visible_text(page, records_text)
        downloaded = self._download_latest_record(
            page,
            variables,
            timeout_seconds=int(action.get("download_ready_timeout_seconds", 300)),
        )
        LOGGER.info("七月项目报表已下载：%s", downloaded)
        if action.get("pause_after_setup", False):
            input("七月项目报表已下载，检查完成后按 Enter：")
        page.goto(list_url, wait_until="domcontentloaded")
        page.get_by_text(project_name, exact=True).first.wait_for(state="visible")
        LOGGER.info("七月项目完成，已自动返回项目列表")

    def _download_content_report(
        self, page: Page, action: dict[str, Any], variables: dict[str, str]
    ) -> None:
        project_name = variables.get("project_name", "未命名项目")
        project_detail_url = variables.get("project_detail_url", page.url)
        skip_field = str(action.get("skip_if_missing_field", "merchant_gmv"))
        if variables.get(skip_field) in (None, "", "na", "null"):
            LOGGER.info("%s 的 %s 为空，跳过内容维度明细下载", project_name, skip_field)
            return
        if page.url != project_detail_url:
            page.goto(project_detail_url, wait_until="domcontentloaded")
            page.get_by_text(str(action.get("detail_text", "查看详情")), exact=False).first.wait_for(
                state="visible"
            )
        self._click_topmost_visible_text(
            page, str(action.get("detail_text", "查看详情")), exact=False
        )
        LOGGER.info("已进入订单详情：%s", project_name)
        self._action(
            page,
            {"type": "click_text", "text": action.get("effect_text", "推广效果"), "exact": False},
            variables,
        )

        detail_heading = page.get_by_text(
            str(action.get("detail_heading", "数据明细")), exact=True
        ).first
        detail_heading.wait_for(state="visible")
        detail_section = detail_heading.locator(
            "xpath=ancestor::*[.//*[contains(normalize-space(.), '下载报表')]][1]"
        )
        self._click_visible_text_in_scope(
            detail_section, str(action.get("dimension_text", "内容维度"))
        )
        LOGGER.info("已进入内容维度：%s", project_name)

        actual_start, actual_end = self._fill_date_range_confirm_each(
            page,
            self._format(str(action["start"]), variables),
            self._format(str(action["end"]), variables),
            str(action.get("separator_text", "至")),
        )
        variables["start_date"] = actual_start
        variables["end_date"] = actual_end
        if action.get("pause_before_attribution", False):
            input("内容维度日期已设置，请检查归因口径控件，完成后按 Enter：")

        attributions = detail_section.get_by_text(
            str(action.get("attribution_text", "归因口径")), exact=False
        )
        for index in range(attributions.count()):
            if attributions.nth(index).is_visible():
                attributions.nth(index).click()
                break
        else:
            raise RuntimeError("数据明细模块中未找到可见的归因口径控件")
        self._action(
            page,
            {"type": "click_text", "text": action.get("attribution_value", "30天"), "exact": True},
            variables,
        )
        LOGGER.info("明细筛选已完成：%s 至 %s，归因口径 30天", actual_start, actual_end)

        self._click_visible_text_in_scope(
            detail_section, str(action.get("download_report_text", "下载报表"))
        )
        confirm = page.get_by_role(
            "button", name=str(action.get("confirm_text", "确定")), exact=True
        )
        confirm.wait_for(state="visible")
        confirm.click()
        self._wait_for_visible_text(
            page, str(action.get("download_records_text", "下载记录"))
        )
        downloaded = self._download_latest_record(
            page,
            variables,
            timeout_seconds=int(action.get("download_ready_timeout_seconds", 300)),
            output_subdir="detail_reports",
            filename_prefix=f"{project_name}_内容维度",
        )
        LOGGER.info("项目内容维度明细已下载：%s", downloaded)
        page.goto(project_detail_url, wait_until="domcontentloaded")
        page.get_by_text("查看全部数据", exact=False).first.wait_for(state="visible")
        LOGGER.info("明细下载完成，已返回项目一级详情：%s", project_name)

    def _open_nested_card_detail(
        self, page: Page, action: dict[str, Any], variables: dict[str, str]
    ) -> None:
        project_name = str(action["project_name"])
        project_title = page.get_by_text(project_name, exact=True).first
        project_title.wait_for(state="visible")
        project_card = project_title.locator(
            "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' mux-card ')][1]"
        )
        project_card.hover()
        project_card.locator("span[data-spm-click*='projectList_detail']").first.click()
        LOGGER.info("已进入特殊项目：%s", project_name)

        child_name = str(action["child_name"])
        child_title = page.get_by_text(child_name, exact=True).first
        child_title.wait_for(state="visible")
        child_card = child_title.locator(
            "xpath=ancestor::*[.//*[contains(normalize-space(.), '查看详情')]][1]"
        )
        child_card.scroll_into_view_if_needed()
        box = child_card.bounding_box()
        if box is None:
            raise RuntimeError(f"无法取得“{child_name}”卡片位置")
        child_card.hover(position={"x": box["width"] / 2, "y": box["height"] * 0.75})
        child_card.get_by_text(
            str(action.get("detail_text", "查看详情")), exact=False
        ).first.click()
        LOGGER.info("已进入嵌套卡片详情：%s", child_name)

        effect_text = str(action.get("effect_text", "推广效果"))
        self._action(
            page,
            {"type": "click_text", "text": effect_text, "exact": False},
            variables,
        )
        LOGGER.info("已点击：%s", effect_text)

        actual_start, actual_end = self._fill_date_range_confirm_each(
            page,
            self._format(str(action["start"]), variables),
            self._format(str(action["end"]), variables),
            str(action.get("separator_text", "")),
            str(action.get("date_picker_selector", "")),
        )
        variables["start_date"] = actual_start
        variables["end_date"] = actual_end
        if action.get("pause_after_date", False):
            input(
                f"日期已设置为 {actual_start} 至 {actual_end}，"
                "请在页面确认日期和数据已刷新，然后按 Enter："
            )

        summary_row = page.locator(
            f'{action.get("date_picker_selector", ".next-range-picker")}:visible'
        ).first.locator(
            "xpath=ancestor::div[contains(@class, 'next-box') and "
            ".//label[contains(normalize-space(.), '归因口径')]][1]"
        )
        attribution_label = summary_row.locator(
            "label.next-input-label",
            has_text=str(action.get("attribution_text", "归因口径")),
        )
        metric_label = str(action.get("metric_label", "商家GMV"))
        previous_metric = self._read_currency_metric(page, metric_label)
        attribution_label.first.wait_for(state="visible")
        attribution_label.first.locator("xpath=ancestor::span[contains(@class, 'next-select')][1]").click()
        LOGGER.info("已点击推广效果汇总区的归因口径")
        self._action(
            page,
            {
                "type": "click_text",
                "text": action.get("attribution_value", "30天"),
                "exact": True,
            },
            variables,
        )
        attribution_value = str(action.get("attribution_value", "30天"))
        selected_value = summary_row.locator(f'em[title="{attribution_value}"]')
        selected_value.first.wait_for(state="visible", timeout=10000)
        deadline = time.monotonic() + float(action.get("metric_refresh_timeout_seconds", 30))
        refreshed_metric = previous_metric
        while time.monotonic() < deadline:
            page.wait_for_timeout(500)
            refreshed_metric = self._read_currency_metric(page, metric_label)
            if refreshed_metric != previous_metric:
                LOGGER.info(
                    "归因切换后 %s 已刷新：%s -> %s",
                    metric_label,
                    previous_metric,
                    refreshed_metric,
                )
                break
        else:
            raise RuntimeError(
                f"归因口径已显示 {attribution_value}，但 {metric_label} 在等待后仍为 {previous_metric}"
            )
        variables["project_name"] = project_name
        self._extract_currency_metric(
            page,
            label=metric_label,
            field=str(action.get("metric_field", "merchant_gmv")),
            variables=variables,
        )
        if action.get("download_content_detail", False):
            self._download_current_content_detail(page, action, variables)
        variables.pop("project_name", None)
        LOGGER.info(
            "特殊项目筛选及提取完成：%s 至 %s，归因口径 %s",
            actual_start,
            actual_end,
            action.get("attribution_value", "30天"),
        )
        if action.get("pause_after_open", False):
            input(f"已完成“{child_name}”筛选及 GMV 提取，检查完成后按 Enter：")

    def _download_current_content_detail(
        self, page: Page, action: dict[str, Any], variables: dict[str, str]
    ) -> None:
        project_name = variables.get("project_name", "未命名项目")
        detail_heading = page.get_by_text(
            str(action.get("detail_heading", "数据明细")), exact=True
        ).first
        detail_heading.wait_for(state="visible")
        detail_section = detail_heading.locator(
            "xpath=ancestor::*[.//*[contains(normalize-space(.), '下载报表')]][1]"
        )
        self._click_visible_text_in_scope(
            detail_section, str(action.get("dimension_text", "内容维度"))
        )
        LOGGER.info("已进入内容维度：%s", project_name)

        actual_start, actual_end = self._fill_date_range_confirm_each(
            page,
            self._format(str(action["start"]), variables),
            self._format(str(action["end"]), variables),
            str(action.get("detail_separator_text", "至")),
        )
        variables["start_date"] = actual_start
        variables["end_date"] = actual_end

        attributions = detail_section.get_by_text(
            str(action.get("attribution_text", "归因口径")), exact=False
        )
        for index in range(attributions.count()):
            if attributions.nth(index).is_visible():
                attributions.nth(index).click()
                break
        else:
            raise RuntimeError("数据明细模块中未找到可见的归因口径控件")
        self._action(
            page,
            {"type": "click_text", "text": action.get("attribution_value", "30天"), "exact": True},
            variables,
        )
        self._click_visible_text_in_scope(
            detail_section, str(action.get("download_report_text", "下载报表"))
        )
        confirm = page.get_by_role(
            "button", name=str(action.get("confirm_text", "确定")), exact=True
        )
        confirm.wait_for(state="visible")
        confirm.click()
        self._wait_for_visible_text(
            page, str(action.get("download_records_text", "下载记录"))
        )
        downloaded = self._download_latest_record(
            page,
            variables,
            timeout_seconds=int(action.get("download_ready_timeout_seconds", 300)),
            output_subdir="detail_reports",
            filename_prefix=f"{project_name}_内容维度",
        )
        LOGGER.info("特殊项目内容维度明细已下载：%s", downloaded)

    @staticmethod
    def _click_visible_text_in_scope(scope: Any, text: str) -> None:
        matches = scope.get_by_text(text, exact=False)
        for index in range(matches.count()):
            if matches.nth(index).is_visible():
                matches.nth(index).click()
                return
        raise RuntimeError(f"指定区域内未找到可见文本“{text}”")

    @staticmethod
    def _click_topmost_visible_text(page: Page, text: str, exact: bool) -> None:
        candidates: list[tuple[float, Any]] = []
        for frame in page.frames:
            matches = frame.get_by_text(text, exact=exact)
            for index in range(matches.count()):
                node = matches.nth(index)
                if not node.is_visible():
                    continue
                box = node.bounding_box()
                if box is not None:
                    candidates.append((box["y"], node))
        if not candidates:
            raise RuntimeError(f"未找到可见文本“{text}”")
        _, target = min(candidates, key=lambda item: item[0])
        target.click()
        LOGGER.info("已点击最上方可见文本：%s", text)

    @staticmethod
    def _wait_for_visible_text(page: Page, text: str, timeout_seconds: int = 30) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            matches = page.get_by_text(text, exact=False)
            for index in range(matches.count()):
                if matches.nth(index).is_visible():
                    return
            page.wait_for_timeout(300)
        raise RuntimeError(f"等待可见文本“{text}”超时")

    def _download_latest_record(
        self,
        page: Page,
        variables: dict[str, str],
        timeout_seconds: int,
        output_subdir: str = "july_reports",
        filename_prefix: str = "戈撒驰7月_内容维度",
    ) -> Path:
        deadline = time.monotonic() + timeout_seconds
        time_pattern = re.compile(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?")
        last_status = ""

        while time.monotonic() < deadline:
            candidates: list[tuple[datetime, Any, str]] = []
            rows = page.locator("tr:visible, [role='row']:visible")
            for index in range(rows.count()):
                row = rows.nth(index)
                text = row.inner_text().strip()
                match = time_pattern.search(text)
                if not match:
                    continue
                raw_time = match.group(0).replace("/", "-")
                date_format = "%Y-%m-%d %H:%M:%S" if raw_time.count(":") == 2 else "%Y-%m-%d %H:%M"
                try:
                    candidates.append((datetime.strptime(raw_time, date_format), row, text))
                except ValueError:
                    continue

            if candidates:
                _, latest_row, last_status = max(candidates, key=lambda item: item[0])
                downloads = latest_row.get_by_text("下载", exact=True)
                for index in range(downloads.count()):
                    if downloads.nth(index).is_visible():
                        with page.expect_download(timeout=30000) as download_info:
                            downloads.nth(index).click()
                        download = download_info.value
                        extension = Path(download.suggested_filename).suffix or ".xlsx"
                        filename = self._safe_filename(
                            f"{filename_prefix}_{variables['start_date']}_{variables['end_date']}_{variables['run_time']}{extension}"
                        )
                        destination = self.output / output_subdir / filename
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        download.save_as(destination)
                        if not destination.exists() or destination.stat().st_size == 0:
                            raise RuntimeError("下载记录已点击，但生成的文件为空")
                        return destination
            page.wait_for_timeout(2000)

        raise RuntimeError(f"等待最新下载记录就绪超时，最新记录内容：{last_status}")

    def _select_date_range(self, page: Page, start: str, end: str, trigger_text: str) -> None:
        trigger = page.get_by_text(trigger_text, exact=True).first
        trigger.wait_for(state="visible")
        trigger.click()

        for value in (start, end):
            selectors = [
                f"[data-value='{value}']",
                f"[data-date='{value}']",
                f"[title='{value}']",
                f"[aria-label='{value}']",
                f"[aria-label*='{value}']",
            ]
            cell = page.locator(", ".join(f"{selector}:visible" for selector in selectors)).first
            try:
                cell.wait_for(state="visible", timeout=5000)
            except PlaywrightTimeout as exc:
                raise RuntimeError(f"日历已打开，但未找到日期单元格 {value}") from exc
            cell.click()
            LOGGER.info("已选择日期：%s", value)

    def _fill_date_range_confirm_each(
        self,
        page: Page,
        start: str,
        end: str,
        separator_text: str,
        date_picker_selector: str = "",
    ) -> tuple[str, str]:
        visible_separators = page.locator(".mux-picker-range-separator:visible")
        if separator_text:
            visible_separators = visible_separators.filter(has_text=separator_text)
        fields: list[Any]
        if date_picker_selector:
            picker = page.locator(f"{date_picker_selector}:visible").filter(
                has=page.locator('input[placeholder="起始日期"]')
            )
            picker.first.wait_for(state="visible", timeout=15000)
            if picker.count() != 1:
                raise RuntimeError(f"顶部日期控件定位异常：匹配到 {picker.count()} 个")
            start_field = picker.first.locator('input[placeholder="起始日期"]')
            end_field = picker.first.locator('input[placeholder="结束日期"]')
            start_field.first.wait_for(state="visible", timeout=15000)
            end_field.first.wait_for(state="visible", timeout=15000)
            if start_field.count() != 1 or end_field.count() != 1:
                raise RuntimeError(
                    "推广效果汇总区日期框定位异常："
                    f"起始日期 {start_field.count()} 个，结束日期 {end_field.count()} 个"
                )
            fields = [start_field.first, end_field.first]
            LOGGER.info("已通过 %s 精确定位推广效果汇总区日期框", date_picker_selector)
        elif visible_separators.count():
            positioned: list[tuple[float, Any]] = []
            for index in range(visible_separators.count()):
                candidate = visible_separators.nth(index)
                box = candidate.bounding_box()
                if box is not None:
                    positioned.append((box["y"], candidate))
            if not positioned:
                raise RuntimeError("可见日期范围分隔符没有可操作位置")
            _, separator = min(positioned, key=lambda item: item[0])
            separator.wait_for(state="visible")
            container = separator.locator("xpath=ancestor::*[count(.//input) >= 2][1]")
            if container.locator("input:visible").count() < 2:
                raise RuntimeError("未能在日期范围控件中找到两个可见输入框")
            fields = [container.locator("input:visible").nth(index) for index in range(2)]
        elif separator_text:
            separator = page.get_by_text(separator_text, exact=True).first
            separator.wait_for(state="visible")
            container = separator.locator("xpath=ancestor::*[count(.//input) >= 2][1]")
            if container.locator("input:visible").count() < 2:
                raise RuntimeError("未能在日期范围控件中找到两个可见输入框")
            fields = [container.locator("input:visible").nth(index) for index in range(2)]
        else:
            date_fields: list[tuple[float, float, Any]] = []
            visible_inputs = page.locator("input:visible")
            for index in range(visible_inputs.count()):
                candidate = visible_inputs.nth(index)
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate.input_value()):
                    continue
                box = candidate.bounding_box()
                if box is not None:
                    date_fields.append((box["y"], box["x"], candidate))
            if len(date_fields) < 2:
                raise RuntimeError("未找到无文字日期范围控件的两个可见日期输入框")
            date_fields.sort(key=lambda item: (item[0], item[1]))
            fields = [date_fields[0][2], date_fields[1][2]]
            LOGGER.info("已按页面位置定位最上方的无文字日期范围控件")

        actual_values: list[str] = []
        for index, (name, value) in enumerate((("开始日期", start), ("结束日期", end))):
            field = fields[index]
            if field.get_attribute("readonly") is not None:
                field.evaluate("element => element.removeAttribute('readonly')")
            field.click()
            field.fill(value)
            field.press("Enter")
            page.wait_for_timeout(800)
            actual = fields[index].input_value()
            if index == 0 and actual != value:
                raise RuntimeError(f"{name}确认失败：期望 {value}，实际 {actual}")
            if index == 1 and actual != value:
                try:
                    requested_date = datetime.strptime(value, "%Y-%m-%d").date()
                    accepted_date = datetime.strptime(actual, "%Y-%m-%d").date()
                except ValueError as exc:
                    raise RuntimeError(f"{name}返回了无效日期：{actual}") from exc
                if accepted_date > requested_date:
                    raise RuntimeError(f"{name}确认异常：期望不晚于 {value}，实际 {actual}")
                LOGGER.warning("平台尚无 %s 数据，采用最近可用日期：%s", value, actual)
            actual_values.append(actual)
            LOGGER.info("%s已输入并按 Enter 确认：%s", name, actual)

        page.wait_for_timeout(1500)
        final_values = [field.input_value() for field in fields]
        if final_values != actual_values:
            raise RuntimeError(
                "日期在确认后被页面回滚："
                f"确认时 {actual_values[0]} 至 {actual_values[1]}，"
                f"最终 {final_values[0]} 至 {final_values[1]}"
            )
        return final_values[0], final_values[1]

    def _extract_currency_metric(
        self,
        page: Page,
        label: str,
        field: str,
        variables: dict[str, str],
    ) -> None:
        normalized = self._read_currency_metric(page, label)
        variables[field] = normalized
        record = {
            "project_name": variables.get("project_name", ""),
            "start_date": variables["start_date"],
            "end_date": variables["end_date"],
            field: normalized,
        }
        self.extracted_records = [
            existing
            for existing in self.extracted_records
            if existing.get("project_name") != record["project_name"]
        ]
        self.extracted_records.append(record)
        LOGGER.info("已提取 %s：%s", label, normalized)
        self._write_extracted_records()

    @staticmethod
    def _read_currency_metric(page: Page, label: str) -> str:
        pattern = r"[¥￥]\s*([0-9][0-9,]*(?:\.\d+)?)"
        for frame in page.frames:
            labels = frame.get_by_text(label, exact=False)
            for index in range(labels.count()):
                node = labels.nth(index)
                if not node.is_visible():
                    continue
                for _level in range(8):
                    text = node.inner_text()
                    match = re.search(pattern, text)
                    lines = [line.strip() for line in text.splitlines()]
                    missing = any(
                        re.fullmatch(r"[¥￥]?\s*[-–—]+", line) is not None
                        for line in lines
                    )
                    if match or missing:
                        return (
                            str(Decimal(match.group(1).replace(",", "")))
                            if match
                            else "na"
                        )
                    node = node.locator("xpath=..")
        raise RuntimeError(f"未能从“{label}”附近提取人民币金额")

    def _load_extracted_records(self) -> list[dict[str, str]]:
        path = self.runtime / "extracted_data.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("已有临时数据无法读取，将在本轮重新生成：%s", path)
            return []
        return data if isinstance(data, list) else []

    def _write_extracted_records(self) -> None:
        if not self.extracted_records:
            return
        path = self.runtime / "extracted_data.json"
        path.write_text(
            json.dumps(self.extracted_records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        LOGGER.info("临时数据已保存：%s", path)

    def _write_manifest(self, entries: list[dict[str, str]], variables: dict[str, str]) -> None:
        directory = self.root / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"manifest_{variables['run_time']}.json"
        path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _format(value: str, variables: dict[str, str]) -> str:
        return value.format_map(variables)

    @staticmethod
    def _safe_filename(value: str) -> str:
        return re.sub(r'[<>:"/\\|?*]', "_", value)


def default_variables(start_date: str | None, end_date: str | None) -> dict[str, str]:
    now = datetime.now()
    today = now.date().isoformat()
    requested_start = start_date or "2026-08-09"
    requested_end = end_date or today
    return {
        "start_date": requested_start,
        "end_date": requested_end,
        "requested_start_date": requested_start,
        "requested_end_date": requested_end,
        "run_date": today,
        "run_time": now.strftime("%Y%m%d_%H%M%S"),
    }
