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
    return parser.parse_args()


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
        collector = Collector(config, config_path)
        files = collector.run(default_variables(args.start_date, args.end_date))
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
