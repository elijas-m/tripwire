"""Entry point: python -m tripwire [--once] [--only id1,id2] [--list-sources]."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from .app import TripwireApp
from .config import Config, ConfigError


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="tripwire", description="Global event early-warning monitor")
    parser.add_argument("--once", action="store_true", help="fetch each source once, process, exit")
    parser.add_argument("--only", default="", help="comma-separated source ids for --once")
    parser.add_argument("--list-sources", action="store_true", help="print enabled sources and exit")
    parser.add_argument("--selftest", action="store_true",
                        help="run synthetic events for every tripwire family and exit")
    parser.add_argument("--selftest-only", default="",
                        help="comma-separated tripwire ids for --selftest")
    parser.add_argument("--backtest", action="store_true",
                        help="replay historical acceptance scenarios (§15) and exit")
    parser.add_argument("--backtest-only", default="",
                        help="comma-separated scenario ids for --backtest")
    parser.add_argument("--backtest-dir", default="", help="scenario directory override")
    parser.add_argument("--config-root", default="", help="project root (default: package parent)")
    parser.add_argument("--log-level", default="", help="override log level")
    return parser.parse_args(argv)


async def amain(args: argparse.Namespace) -> int:
    cfg = Config(Path(args.config_root) if args.config_root else None).load()
    setup_logging(args.log_level or cfg.app.get("log_level", "INFO"))
    log = logging.getLogger("tripwire")

    if args.list_sources:
        for source in cfg.enabled_sources():
            print(f"{source.id:26} tier={source.tier} {source.interval_s:>5}s "
                  f"{source.theater:9} {source.type:8} {source.name}")
        return 0

    # One-shot CLI modes must not start a second getUpdates long-poll: while the
    # service runs, both pollers would consume each other's updates (HTTP 409).
    one_shot = args.once or args.selftest or args.backtest
    app = TripwireApp(cfg, commands=not one_shot)
    await app.start()
    loop = asyncio.get_running_loop()
    try:
        if args.selftest:
            from .selftest import run_selftest

            only = [s.strip() for s in args.selftest_only.split(",") if s.strip()]
            report = await run_selftest(app, only=only or None)
            print(report["summary"])
            return 0 if all(item["ok"] for item in report["results"]) else 1

        if args.backtest:
            from .backtest import run_backtest

            only = [s.strip() for s in args.backtest_only.split(",") if s.strip()]
            directory = Path(args.backtest_dir) if args.backtest_dir else None
            report = await run_backtest(app, only=only or None, directory=directory)
            print(report["summary"])
            return 0 if report["passed"] == len(report["results"]) else 1

        if args.once:
            only = [s.strip() for s in args.only.split(",") if s.strip()]
            await app.run_once(only or None)
            return 0

        for name in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, app.stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        sig_hup = getattr(signal, "SIGHUP", None)
        if sig_hup is not None:
            try:
                loop.add_signal_handler(sig_hup, lambda: asyncio.create_task(app.reload_config()))
            except (NotImplementedError, RuntimeError):
                pass
        await app.run_forever()
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        app.stop.set()
        await app.close()
        log.info("exit")


def main() -> None:
    args = parse_args()
    try:
        code = asyncio.run(amain(args))
    except ConfigError as exc:
        setup_logging("INFO")
        logging.getLogger("tripwire").error("configuration error: %s", exc)
        code = 2
    sys.exit(code)


if __name__ == "__main__":
    main()
