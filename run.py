#!/usr/bin/env python3
"""
Stock Monitor — daemon entry point.

Usage
─────
    python run.py                          # use default config/config.yaml
    python run.py --config /my/config.yaml
"""

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Real-time stock monitoring daemon with TradingView webhook support."
    )
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        metavar="PATH",
        help="Path to config YAML file (default: config/config.yaml)",
    )
    args = parser.parse_args()

    # Lazy import so errors appear after argparse help
    try:
        from stock_monitor.main import run_app
    except ImportError as exc:
        print(f"ERROR: missing dependencies — run:  pip install -r requirements.txt\n({exc})")
        sys.exit(1)

    run_app(args.config)


if __name__ == "__main__":
    main()
