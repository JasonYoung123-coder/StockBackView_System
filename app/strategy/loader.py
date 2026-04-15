from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from app.core.config import get_settings
from app.strategy.base import BaseStrategy


class StrategyLoadError(RuntimeError):
    pass


@dataclass
class LoadedStrategy:
    name: str
    description: str
    path: Path
    adapted: bool
    instance: BaseStrategy
    config_schema: dict | None = None


class StrategyLoader:
    def __init__(self) -> None:
        self.settings = get_settings()

    def _strategy_files(self) -> list[Path]:
        return sorted(
            path
            for path in self.settings.strategies_dir.glob("*.py")
            if path.is_file() and path.name != "__init__.py"
        )

    def _load_module(self, path: Path) -> ModuleType:
        module_name = f"strategy_{path.stem}_{path.stat().st_mtime_ns}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise StrategyLoadError(f"无法导入策略文件: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _instantiate_strategy(self, strategy_cls, params: dict | None = None) -> BaseStrategy:
        params = params or {}
        try:
            return strategy_cls(**params)
        except TypeError:
            instance = strategy_cls()
            for key, value in params.items():
                if hasattr(instance, key):
                    setattr(instance, key, value)
            return instance

    def _build_strategy(self, path: Path, params: dict | None = None) -> LoadedStrategy:
        module = self._load_module(path)
        strategy_cls = getattr(module, "Strategy", None)
        if not isinstance(strategy_cls, type) or not issubclass(strategy_cls, BaseStrategy):
            raise StrategyLoadError(f"策略文件 {path.name} 未暴露 BaseStrategy 子类 Strategy。")

        instance = self._instantiate_strategy(strategy_cls, params=params)
        config_schema = instance.get_config_schema() if hasattr(instance, "get_config_schema") else None
        return LoadedStrategy(
            name=getattr(instance, "name", path.stem),
            description=getattr(instance, "description", ""),
            path=path,
            adapted=False,
            instance=instance,
            config_schema=config_schema,
        )

    def list_strategies(self) -> list[LoadedStrategy]:
        strategies: list[LoadedStrategy] = []
        errors: list[str] = []
        for path in self._strategy_files():
            try:
                strategies.append(self._build_strategy(path))
            except Exception as exc:
                errors.append(f"{path.name}: {exc}")
        if not strategies and errors:
            raise StrategyLoadError("未发现可用策略。\n" + "\n".join(errors))
        return strategies

    def get_strategy(self, name: str, params: dict | None = None) -> LoadedStrategy:
        for path in self._strategy_files():
            if path.stem == name:
                return self._build_strategy(path, params=params)

        for strategy in self.list_strategies():
            if strategy.name == name or strategy.path.stem == name:
                return self._build_strategy(strategy.path, params=params)
        raise StrategyLoadError(f"未找到策略: {name}")
