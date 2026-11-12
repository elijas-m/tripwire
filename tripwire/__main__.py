"""Entry point: python -m tripwire [--once] [--only id1,id2] [--list-sources]."""

from __future__ import annotations

import argparse
import asyncio
import json
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
    parser.add_argument("--backtest-json", default="",
                        help="write the point-in-time backtest report as JSON")
    parser.add_argument("--export-labels", action="store_true",
                        help="export blind labeling candidates from the database and exit")
    parser.add_argument("--labels-out", default="backtest/labels/pilot",
                        help="directory for candidates.jsonl, sealed.jsonl and manifest.json")
    parser.add_argument("--labels-since-days", type=int, default=30,
                        help="only clusters first seen within this many days")
    parser.add_argument("--labels-negatives", type=int, default=0,
                        help="random negative candidates per theater")
    parser.add_argument("--labels-signal-per-band", type=int, default=20,
                        help="cap candidates per anchor-score band across all theaters")
    parser.add_argument("--labels-alert-cap", type=int, default=0,
                        help="cap alert candidates per theater (0 = all)")
    parser.add_argument("--labels-seed", default="tripwire",
                        help="seed for deterministic negative sampling")
    parser.add_argument("--labels-min-age-hours", type=float, default=0.0,
                        help="only export episodes at least this many hours old")
    parser.add_argument("--labels-require-anchor", action="store_true",
                        help="only export clusters with a frozen score_initial")
    parser.add_argument("--labels-exclude", action="append", default=[],
                        help="file or directory with completed labels to skip")
    parser.add_argument("--labels-force", action="store_true",
                        help="overwrite an existing export directory")
    parser.add_argument("--merge-labels", action="store_true",
                        help="merge dated label rounds into one calibration pair and exit")
    parser.add_argument("--labels-merge-root", default="backtest/labels/m2",
                        help="root with dated label rounds")
    parser.add_argument("--labels-merge-out", default="",
                        help="merged output directory (default: <root>/aggregate)")
    parser.add_argument("--calibrate-labels", action="store_true",
                        help="compute Brier calibration from labels and sealed scores and exit")
    parser.add_argument("--calibration-dir", default="backtest/labels/pilot",
                        help="directory with labels.jsonl and sealed.jsonl")
    parser.add_argument("--calibration-labels", default="labels.jsonl",
                        help="label file name inside the calibration directory")
    parser.add_argument("--calibration-sealed", default="sealed.jsonl",
                        help="sealed score file name inside the calibration directory")
    parser.add_argument("--calibration-min-records", type=int, default=100,
                        help="minimum episodes per horizon before publishing Brier")
    parser.add_argument("--calibration-min-positives", type=int, default=20,
                        help="minimum positives per horizon before publishing Brier")
    parser.add_argument("--calibration-bootstrap", type=int, default=1000,
                        help="bootstrap iterations for confidence intervals")
    parser.add_argument("--calibration-json", default="",
                        help="write the calibration report as JSON")
    parser.add_argument("--kappa-labels", action="store_true",
                        help="compute inter-annotator kappa on case_type and exit")
    parser.add_argument("--kappa-dir", default="backtest/labels/m2",
                        help="round directory with both label files")
    parser.add_argument("--kappa-user-file", default="labels.jsonl",
                        help="user label file name inside the round directory")
    parser.add_argument("--kappa-agent-file", default="labels.agent.jsonl",
                        help="agent label file name inside the round directory")
    parser.add_argument("--kappa-json", default="",
                        help="write the agreement report as JSON")
    parser.add_argument("--kappa-bootstrap", type=int, default=1000,
                        help="bootstrap iterations for the kappa confidence interval")
    parser.add_argument("--kappa-seed", type=int, default=42,
                        help="bootstrap seed")
    parser.add_argument("--kappa-min-pairs", type=int, default=5,
                        help="minimum paired labels before reporting kappa")
    parser.add_argument("--kappa-threshold", type=float, default=0.7,
                        help="kappa threshold for the dictionary verdict")
    parser.add_argument("--coverage-labels", action="store_true",
                        help="build superseding templates for negatives without 168 h coverage and exit")
    parser.add_argument("--coverage-round", default="",
                        help="round directory with candidates.jsonl and labels.jsonl")
    parser.add_argument("--coverage-out", default="",
                        help="output directory (default: <root>/coverage/<round>)")
    parser.add_argument("--coverage-hours", type=int, default=168,
                        help="horizon the negative labels must cover")
    parser.add_argument("--coverage-force", action="store_true",
                        help="overwrite an existing coverage template")
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

    if args.export_labels:
        from .db import DB
        from .labels import export_label_candidates

        db_path = cfg.data_dir() / "tripwire.db"
        if not db_path.exists():
            log.error("label export: database not found at %s", db_path)
            return 2
        db = DB(db_path)
        await db.open()
        try:
            result = await export_label_candidates(
                db, Path(args.labels_out),
                since_days=args.labels_since_days,
                negatives_per_theater=args.labels_negatives,
                signal_per_band=args.labels_signal_per_band,
                alert_cap=args.labels_alert_cap,
                seed=args.labels_seed,
                min_age_hours=args.labels_min_age_hours,
                require_anchor=args.labels_require_anchor,
                exclude_paths=args.labels_exclude,
                force=args.labels_force,
            )
        except (FileExistsError, FileNotFoundError, ValueError) as exc:
            log.error("label export: %s", exc)
            return 2
        finally:
            await db.close()
        print(f"label export: {result['counts']} -> {result['out']}")
        return 0

    if args.merge_labels:
        from .labels import merge_label_rounds

        root = Path(args.labels_merge_root)
        out = Path(args.labels_merge_out) if args.labels_merge_out else root / "aggregate"
        try:
            result = merge_label_rounds(root, out)
        except (FileNotFoundError, ValueError) as exc:
            log.error("label merge: %s", exc)
            return 2
        print(
            f"Слияние меток: раундов {len(result['rounds'])}, "
            f"меток {result['labels']} из {result['labels_raw']}, "
            f"sealed {result['sealed']}, "
            f"конфликтов меток {result['conflicting_labels']}, "
            f"конфликтов sealed {result['conflicting_sealed']} -> {result['out']}"
        )
        return 0

    if args.calibrate_labels:
        from .calibration import calibrate_files, format_report

        directory = Path(args.calibration_dir)
        labels_path = directory / args.calibration_labels
        sealed_path = directory / args.calibration_sealed
        try:
            report = calibrate_files(
                labels_path, sealed_path,
                min_records=args.calibration_min_records,
                min_positives=args.calibration_min_positives,
                bootstrap=args.calibration_bootstrap,
            )
        except FileNotFoundError as exc:
            log.error("calibration: %s", exc)
            return 2
        except ValueError as exc:
            log.error("calibration: %s", exc)
            return 2
        print(format_report(report))
        if args.calibration_json:
            out = Path(args.calibration_json)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
        return 0

    if args.kappa_labels:
        from .agreement import agreement_files, format_agreement

        directory = Path(args.kappa_dir)
        try:
            report = agreement_files(
                directory / args.kappa_user_file,
                directory / args.kappa_agent_file,
                bootstrap=args.kappa_bootstrap,
                seed=args.kappa_seed,
                min_pairs=args.kappa_min_pairs,
                threshold=args.kappa_threshold,
            )
        except (FileNotFoundError, ValueError) as exc:
            log.error("kappa: %s", exc)
            return 2
        print(format_agreement(report))
        if args.kappa_json:
            out = Path(args.kappa_json)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
        return 0

    if args.coverage_labels:
        from .coverage import build_coverage_template

        if not args.coverage_round:
            log.error("coverage: --coverage-round is required")
            return 2
        try:
            result = build_coverage_template(
                Path(args.coverage_round),
                Path(args.coverage_out) if args.coverage_out else None,
                hours=args.coverage_hours,
                force=args.coverage_force,
            )
        except (FileExistsError, FileNotFoundError, ValueError) as exc:
            log.error("coverage: %s", exc)
            return 2
        print(f"Покрытие {result['horizon_hours']} ч: меток {result['candidates']}, "
              f"ждут закрытия окна {result['pending']} -> {result['out']}")
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
            export = Path(args.backtest_json) if args.backtest_json else None
            report = await run_backtest(app, only=only or None, directory=directory,
                                        export=export)
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
