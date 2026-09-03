from __future__ import annotations

import logging
import json
import re
from datetime import date, datetime
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
        elif action_type == "locate_texts":
            for raw_text in action["texts"]:
                expected = self._format(str(raw_text), variables)
                matches = page.get_by_text(expected, exact=bool(action.get("exact", True)))
                matches.first.wait_for(state="visible")
                LOGGER.info("已定位字段：%s（匹配 %s 个）", expected, matches.count())
        elif action_type == "visit_details":
            for raw_text in action["texts"]:
                expected = self._format(str(raw_text), variables)
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
                detail_icon.click()
                LOGGER.info("已进入详情：%s", expected)
                if action.get("pause_each", False):
                    input(f"已进入“{expected}”详情，检查完成后按 Enter 返回列表：")
                page.go_back(wait_until="domcontentloaded")
                page.get_by_text(expected, exact=True).first.wait_for(state="visible")
                LOGGER.info("已返回项目列表：%s", expected)
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
    today = date.today().isoformat()
    now = datetime.now()
    return {
        "start_date": start_date or today,
        "end_date": end_date or today,
        "run_date": today,
        "run_time": now.strftime("%Y%m%d_%H%M%S"),
    }
