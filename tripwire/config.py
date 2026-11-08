"""Configuration loading and validation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from .models import Source

KNOWN_PARSERS = {
    "rss", "gnews_rss", "usgs_geojson", "emsc_geojson", "emsc_ws", "ingv_geojson",
    "gdacs_rss", "tsunami_atom", "hans_json", "html_links", "html_table",
    "imo_quakes", "vaac_tokyo", "vaac_messages", "vaac_list",
    "volcano_status_html", "jma_eqvol", "vona_jina",
    "who_don_odata", "ukrainealarm_json", "tzeva_json",
    "adsb_json", "ews_dashboard", "safecast_json", "tradingview_json", "eia_json",
    "frankfurter_json",
    "spa_next_data",
    "telegram_html", "ppi_telegram", "ukmto_jina", "oref_json",
    "easa_czib", "travel_advisory", "portwatch_json", "gpsjam_csv", "opensky_json",
}

KNOWN_TYPES = {"rss", "json", "html", "ws", "telegram", "market", "safecast"}
KNOWN_THEATERS = {"EU_RU", "ISRAEL", "GULF", "MENA", "PANDEMIC", "NATURE", "MARKETS"}


class ConfigError(RuntimeError):
    pass


def _read_yaml(path: Path, required: bool = True) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ConfigError(f"missing config file: {path}")
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


class Config:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or Path(__file__).resolve().parent.parent).resolve()
        self.config_dir = self.root / "config"
        self.secrets_path = Path(os.environ.get("TRIPWIRE_SECRETS", "/etc/tripwire/secrets.env"))
        self.data_dir_override: Path | str | None = None
        self._loaded = False

        self.app: dict[str, Any] = {}
        self.telegram_cfg: dict[str, Any] = {}
        self.alerts_cfg: dict[str, Any] = {}
        self.files: dict[str, str] = {}
        self.sources: list[Source] = []
        self.sources_by_id: dict[str, Source] = {}
        self.tripwires: list[dict[str, Any]] = []
        self.lexicon: dict[str, Any] = {}
        self.thresholds: dict[str, Any] = {}
        self.geofences: dict[str, Any] = {}
        self.gazetteer: dict[str, Any] = {}
        self.channels: dict[str, Any] = {}
        self.pizza: dict[str, Any] = {}
        self.secrets: dict[str, str] = {}

    # ---------- loading ----------
    def load(self) -> "Config":
        self._load_secrets()
        self.app_cfg = _read_yaml(self.config_dir / "config.yaml")
        self.app = self.app_cfg.get("app", {})
        self.telegram_cfg = self.app_cfg.get("telegram", {})
        self.alerts_cfg = self.app_cfg.get("alerts", {})
        self.files = self.app_cfg.get("files", {})
        translate_cfg = self.app_cfg.get("translate")
        if (isinstance(translate_cfg, dict)
                and os.environ.get("TRIPWIRE_TRANSLATE", "").strip().lower()
                in ("0", "false", "no", "off")):
            translate_cfg["enabled"] = False

        def f(name: str, default: str) -> Path:
            return self.config_dir / self.files.get(name, default)

        src_raw = _read_yaml(f("sources", "sources.yaml"))
        defaults = src_raw.get("defaults", {})
        self.sources = [Source.from_dict(item, defaults) for item in src_raw.get("sources", [])]
        self.tripwires = _read_yaml(f("tripwires", "tripwires.yaml")).get("tripwires", [])
        self.lexicon = _read_yaml(f("lexicon", "lexicon.yaml"))
        self.thresholds = _read_yaml(f("thresholds", "thresholds.yaml"))
        self.geofences = _read_yaml(f("geofences", "geofences.yaml"))
        self.gazetteer = _read_yaml(f("gazetteer", "gazetteer.yaml"))
        self.channels = _read_yaml(f("channels", "channels.yaml"))
        self.pizza = _read_yaml(f("pizza", "pizza.yaml"))

        self.validate()
        self._loaded = True
        return self

    def _load_secrets(self) -> None:
        self.secrets = dict(parse_env_file(self.secrets_path))
        for key in (
            "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
            "TG_THREAD_CRITICAL", "TG_THREAD_WARNING", "TG_THREAD_NATURE",
            "TG_THREAD_HEALTH", "TG_THREAD_DIGEST", "TG_THREAD_TEST",
            "TRIPWIRE_ADMINS", "NTFY_TOPIC", "SMTP_USER", "SMTP_PASSWORD",
            "OPENROUTER_API_KEY",
        ):
            if os.environ.get(key):
                self.secrets[key] = os.environ[key]

    def validate(self) -> None:
        if not self.sources:
            raise ConfigError("sources.yaml: no sources")
        ids: set[str] = set()
        self.sources_by_id = {}
        for src in self.sources:
            if not src.id:
                raise ConfigError("source without id")
            if src.id in ids:
                raise ConfigError(f"duplicate source id: {src.id}")
            ids.add(src.id)
            if src.type not in KNOWN_TYPES:
                raise ConfigError(f"{src.id}: unknown type {src.type}")
            if src.parser not in KNOWN_PARSERS:
                raise ConfigError(f"{src.id}: unknown parser {src.parser}")
            if src.enabled and not src.url:
                raise ConfigError(f"{src.id}: enabled source without url")
            if src.theater not in KNOWN_THEATERS:
                raise ConfigError(f"{src.id}: unknown theater {src.theater}")
            if not 0 <= src.tier <= 3:
                raise ConfigError(f"{src.id}: tier must be 0..3")
            self.sources_by_id[src.id] = src

        tw_ids: set[str] = set()
        for tw in self.tripwires:
            tid = tw.get("id")
            if not tid:
                raise ConfigError("tripwire without id")
            if tid in tw_ids:
                raise ConfigError(f"duplicate tripwire id: {tid}")
            tw_ids.add(tid)
            if tw.get("type") not in ("regex", "numeric", "handler"):
                raise ConfigError(f"{tid}: unknown type {tw.get('type')}")
            if not tw.get("level"):
                raise ConfigError(f"{tid}: level required")
            if str(tw["level"]).upper() not in ("CRITICAL", "WARNING", "WATCH", "L3", "L2", "L1"):
                raise ConfigError(f"{tid}: bad level {tw['level']}")
            for sid in tw.get("source_ids", []):
                if sid not in ids:
                    raise ConfigError(f"{tid}: unknown source_id {sid}")

        levels = self.thresholds.get("levels", {})
        for key in ("watch", "warning", "critical"):
            if key not in levels:
                raise ConfigError(f"thresholds.yaml: missing levels.{key}")
        if not (levels["watch"] < levels["warning"] < levels["critical"]):
            raise ConfigError("thresholds must satisfy watch < warning < critical")

        for theater in self.geofences.get("theaters", {}):
            if theater not in KNOWN_THEATERS:
                raise ConfigError(f"geofences: unknown theater {theater}")

    # ---------- accessors ----------
    def enabled_sources(self) -> list[Source]:
        return [s for s in self.sources if s.enabled]

    def secret(self, key: str, default: str | None = None) -> str | None:
        return self.secrets.get(key, default)

    def data_dir(self) -> Path:
        override = os.environ.get("TRIPWIRE_DATA_DIR")
        if self.data_dir_override:
            return Path(self.data_dir_override)
        path = Path(override) if override else Path(self.app.get("data_dir", "/var/lib/tripwire"))
        return path

    def g(self, *keys: str, default: Any = None) -> Any:
        """Nested getter over thresholds/geofences."""
        node: Any = self.thresholds
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def level_threshold(self, level: int) -> float:
        key = {1: "watch", 2: "warning", 3: "critical"}.get(level, "critical")
        return float(self.thresholds["levels"][key])

    def threshold_for_name(self, name: str) -> float:
        return float(self.thresholds["levels"][name.lower()])

    def channel_topic(self, name: str) -> str | None:
        topics = self.channels.get("telegram", {}).get("topics", {})
        entry = topics.get(name)
        if not entry:
            return None
        env_key = entry.get("thread_id_env")
        value = self.secret(env_key) if env_key else None
        return str(value) if value else None

    def admin_ids(self) -> set[int]:
        raw = self.secret(self.channels.get("telegram", {}).get("admins_env", "TRIPWIRE_ADMINS"), "") or ""
        out: set[int] = set()
        for part in raw.replace(";", ",").split(","):
            part = part.strip()
            if part.isdigit():
                out.add(int(part))
        return out

    def fingerprint(self) -> str:
        parts = []
        for name in ("config.yaml", "sources.yaml", "tripwires.yaml", "lexicon.yaml",
                     "thresholds.yaml", "geofences.yaml", "gazetteer.yaml", "channels.yaml", "pizza.yaml"):
            path = self.config_dir / name
            if path.exists():
                parts.append(f"{name}:{path.stat().st_mtime_ns}:{path.stat().st_size}")
        return "|".join(parts)
