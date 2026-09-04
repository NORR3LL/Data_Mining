from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from rpa_collector.collector import Collector, default_variables
from rpa_collector.config import load_config
from rpa_collector.reporting import generate_gmv_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="网页数据与报表 RPA 采集程序")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--start-date", help="开始日期，例如 2026-09-01")
    parser.add_argument("--end-date", help="结束日期，例如 2026-09-03")
    parser.add_argument(
        "--inspect-first-project",
        action="store_true",
        help="仅打开第一个标准项目并停在数据页，供人工检查页面元素",
    )
    parser.add_argument(
        "--inspect-sixth-project",
        action="store_true",
        help="仅运行第六个特殊项目并停在推广效果页面，供人工检查",
    )
    parser.add_argument(
        "--inspect-standard-project",
        type=int,
        choices=range(1, 5),
        metavar="1-4",
        help="仅运行指定序号的标准项目，并在归因选择前停住",
    )
    return parser.parse_args()


def enable_first_project_inspection(config: dict) -> None:
    found = False
    for action in config["site"].get("post_login_actions", []):
        if action.get("type") == "click_role" and not found:
            continue
        if action.get("type") == "visit_details" and not found:
            texts = action.get("texts", [])
            if not texts:
                continue
            action["enabled"] = True
            action["texts"] = texts[:1]
            action["pause_each"] = True
            found = True
            continue
        action["enabled"] = False
    if not found:
        raise ValueError("配置中未找到可用于检查的标准项目")


def enable_standard_project_inspection(config: dict, project_index: int) -> None:
    enable_first_project_inspection(config)
    for action in config["site"].get("post_login_actions", []):
        if action.get("type") != "visit_details" or not action.get("enabled", True):
            continue
        original = load_config(Path("config.yaml"))["site"]["post_login_actions"][1]["texts"]
        action["texts"] = [original[project_index - 1]]
        action["pause_each"] = True
        for detail_action in action.get("detail_actions", []):
            if detail_action.get("type") == "download_content_report":
                detail_action["pause_before_attribution"] = True
        return


def enable_sixth_project_inspection(config: dict) -> None:
    found = False
    for action in config["site"].get("post_login_actions", []):
        if action.get("type") == "click_role" and not found:
            continue
        if action.get("type") == "open_nested_card_detail":
            action["enabled"] = True
            action["pause_after_open"] = True
            found = True
            continue
        action["enabled"] = False
    if not found:
        raise ValueError("配置中未找到第六个特殊项目")


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler("collector.log", encoding="utf-8")],
    )
    try:
        config_path = Path(args.config)
        config = load_config(config_path)
        if args.inspect_standard_project:
            enable_standard_project_inspection(config, args.inspect_standard_project)
        elif args.inspect_first_project:
            enable_first_project_inspection(config)
        elif args.inspect_sixth_project:
            enable_sixth_project_inspection(config)
        collector = Collector(config, config_path)
        files = collector.run(default_variables(args.start_date, args.end_date))
        if not args.inspect_first_project and not args.inspect_sixth_project and not args.inspect_standard_project:
            files.append(generate_gmv_report(config_path.resolve().parent, config))
        print("\n采集完成：")
        for file in files:
            print(f"- {file.resolve()}")
        return 0
    except Exception as exc:
        logging.exception("采集失败：%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
