from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class AppConfig:
    base_dir: Path
    config_path: Path
    data_dir: Path
    cache_dir: Path
    strategies_dir: Path
    tushare_token: str
    default_commission_rate: float
    default_stamp_duty_rate: float
    benchmarks: dict[str, str]
    qmt_xtquant_path: str
    qmt_userdata_path: str
    qmt_account_id: str
    qmt_account_type: str
    feishu_app_id: str
    feishu_app_secret: str
    feishu_chat_id: str
    feishu_enabled: bool


DEFAULT_BENCHMARKS = {
    "上证指数": "000001.SH",
    "沪深300": "000300.SH",
    "中证1000": "000852.SH",
}


_CACHED_SETTINGS: AppConfig | None = None
_CACHED_SIGNATURE: tuple[bool, int] | None = None


def _resolve_path(base_dir: Path, raw: str, fallback: str) -> Path:
    path = Path((raw or fallback).strip())
    return path if path.is_absolute() else base_dir / path


def get_settings() -> AppConfig:
    base_dir = Path(__file__).resolve().parents[2]
    config_path = base_dir / "config" / "config.toml"
    signature = (config_path.exists(), config_path.stat().st_mtime_ns if config_path.exists() else 0)
    global _CACHED_SETTINGS, _CACHED_SIGNATURE
    if _CACHED_SETTINGS is not None and _CACHED_SIGNATURE == signature:
        return _CACHED_SETTINGS

    parsed: dict = {}

    if config_path.exists():
        with config_path.open("rb") as handle:
            parsed = tomllib.load(handle)

    tushare_section = parsed.get("tushare", {})
    data_section = parsed.get("data", {})
    strategy_section = parsed.get("strategy", {})
    backtest_section = parsed.get("backtest", {})
    benchmark_section = parsed.get("benchmarks", {})
    qmt_section = parsed.get("qmt", {})
    feishu_section = parsed.get("feishu", {})

    data_dir = _resolve_path(base_dir, data_section.get("dir", "data"), "data")
    cache_dir = _resolve_path(data_dir, data_section.get("cache_dir", "cache"), "cache")
    strategies_dir = _resolve_path(base_dir, strategy_section.get("dir", "strategies"), "strategies")

    data_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    strategies_dir.mkdir(parents=True, exist_ok=True)

    benchmarks = dict(DEFAULT_BENCHMARKS)
    benchmarks.update({str(k): str(v) for k, v in benchmark_section.items() if v})

    settings = AppConfig(
        base_dir=base_dir,
        config_path=config_path,
        data_dir=data_dir,
        cache_dir=cache_dir,
        strategies_dir=strategies_dir,
        tushare_token=str(tushare_section.get("token", "")).strip(),
        default_commission_rate=float(backtest_section.get("default_commission_rate", 0.0003)),
        default_stamp_duty_rate=float(backtest_section.get("default_stamp_duty_rate", 0.001)),
        benchmarks=benchmarks,
        qmt_xtquant_path=str(qmt_section.get("xtquant_path", "")).strip(),
        qmt_userdata_path=str(qmt_section.get("userdata_path", "")).strip(),
        qmt_account_id=str(qmt_section.get("account_id", "")).strip(),
        qmt_account_type=str(qmt_section.get("account_type", "STOCK")).strip(),
        feishu_app_id=str(feishu_section.get("app_id", "")).strip(),
        feishu_app_secret=str(feishu_section.get("app_secret", "")).strip(),
        feishu_chat_id=str(feishu_section.get("chat_id", "")).strip(),
        feishu_enabled=bool(feishu_section.get("enabled", True)),
    )
    _CACHED_SETTINGS = settings
    _CACHED_SIGNATURE = signature
    return settings


def require_tushare_token() -> str:
    token = get_settings().tushare_token
    if not token:
        raise ConfigError("未在 config/config.toml 中配置 Tushare Token，请先填写 token。")
    return token
