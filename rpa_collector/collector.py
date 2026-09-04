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
        self.extracted_records: list[dict[str, str]] = []

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
        elif action_type == "open_nested_card_detail":
            self._open_nested_card_detail(page, action)
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
        if action.get("pause_after_setup", True):
            input("七月项目报表已下载，检查完成后按 Enter：")

    def _open_nested_card_detail(self, page: Page, action: dict[str, Any]) -> None:
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
            {},
        )
        LOGGER.info("已点击：%s", effect_text)
        if action.get("pause_after_open", True):
            input(f"已进入“{child_name}”详情并点击“{effect_text}”，检查完成后按 Enter：")

    @staticmethod
    def _click_visible_text_in_scope(scope: Any, text: str) -> None:
        matches = scope.get_by_text(text, exact=False)
        for index in range(matches.count()):
            if matches.nth(index).is_visible():
                matches.nth(index).click()
                return
        raise RuntimeError(f"指定区域内未找到可见文本“{text}”")

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
                            f"戈撒驰7月_内容维度_{variables['start_date']}_{variables['end_date']}_{variables['run_time']}{extension}"
                        )
                        destination = self.output / "july_reports" / filename
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
        self, page: Page, start: str, end: str, separator_text: str
    ) -> tuple[str, str]:
        visible_separators = page.locator(".mux-picker-range-separator:visible").filter(
            has_text=separator_text
        )
        separator = (
            visible_separators.first
            if visible_separators.count()
            else page.get_by_text(separator_text, exact=True).first
        )
        separator.wait_for(state="visible")
        container = separator.locator("xpath=ancestor::*[count(.//input) >= 2][1]")
        if container.locator("input:visible").count() < 2:
            raise RuntimeError("未能在日期范围控件中找到两个可见输入框")

        actual_values: list[str] = []
        for index, (name, value) in enumerate((("开始日期", start), ("结束日期", end))):
            field = container.locator("input:visible").nth(index)
            if field.get_attribute("readonly") is not None:
                field.evaluate("element => element.removeAttribute('readonly')")
            field.click()
            field.fill(value)
            field.press("Enter")
            page.wait_for_timeout(800)
            actual = container.locator("input:visible").nth(index).input_value()
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
        return actual_values[0], actual_values[1]

    def _extract_currency_metric(
        self,
        page: Page,
        label: str,
        field: str,
        variables: dict[str, str],
    ) -> None:
        pattern = r"[¥￥]\s*([0-9][0-9,]*(?:\.\d+)?)"
        for frame in page.frames:
            labels = frame.get_by_text(label, exact=False)
            for index in range(labels.count()):
                node = labels.nth(index)
                for _level in range(8):
                    text = node.inner_text()
                    match = re.search(pattern, text)
                    lines = [line.strip() for line in text.splitlines()]
                    missing = any(
                        re.fullmatch(r"[¥￥]?\s*[-–—]+", line) is not None
                        for line in lines
                    )
                    if match or missing:
                        normalized = (
                            str(Decimal(match.group(1).replace(",", "")))
                            if match
                            else "na"
                        )
                        self.extracted_records.append(
                            {
                                "project_name": variables.get("project_name", ""),
                                "start_date": variables["start_date"],
                                "end_date": variables["end_date"],
                                field: normalized,
                            }
                        )
                        LOGGER.info("已提取 %s：%s", label, normalized)
                        self._write_extracted_records()
                        return
                    node = node.locator("xpath=..")
        raise RuntimeError(f"未能从“{label}”附近提取人民币金额")

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
    return {
        "start_date": start_date or "2026-08-09",
        "end_date": end_date or today,
        "run_date": today,
        "run_time": now.strftime("%Y%m%d_%H%M%S"),
    }
