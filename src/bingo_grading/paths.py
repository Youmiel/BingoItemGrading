"""数据根目录解析。

本项目依赖三类外部数据，来源与生命周期都不同，混在一处是过去「路径散落、
必须挂 junction」的根源。这里把它们分开：

| 类别 | 位置 | 性质 | 是否入库 |
|---|---|---|---|
| 数据包视图 | `data/upstream/<pack>/datapack/` | 原版 ⊗ 地形包 overlay 的**合并结果** | 否（由 `bg data sync` 复制） |
| 上游产物 | `data/upstream/<pack>/` | RecipeAnalysis / WGS 的**产出** | 否（可由 `bg data sync` 重建） |
| 上游工作目录 | `config.yml` 里的两个项目根 | 别人项目的源码与 jar / 世界存档 | — |

运行态的解析顺序是：**显式参数 > `data/` 自持**。`config.yml` 指向的上游只在
`bg data sync` / `bg data verify`（本模块的 `upstream_*` 与 `upstream.py`）里使用；
`ref/` 是 gitignore 目录、不可迁移，所以**不做运行时默认值**——缺 `data/` 就是
`MISSING`，由调用方报缺口，而不是悄悄去读开发者本机的上游项目。

术语：`pack` 是地形包/原版版本号（如 `26.3`），与 `WorldStat.pack_version` 同一个值。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config as config_module

ROOT = Path(__file__).resolve().parents[2]

# ------------------------------------------------------------------ 配置
#
# 只有**两个上游项目**的位置来自 `config.yml`（不入库），而且没有默认值：
# 缺文件、缺项、空值都在第一次读取时抛 `config.ConfigError`，命令随即报错退出。
# 用模块级 `__getattr__` 推迟到首次访问，是为了让 `bg config` 能把「缺什么」
# 报出来，而不是在 import 阶段甩一段 traceback。
#
# 本仓库自己的目录固定在下面，不参与配置：它们就是仓库的一部分，可配只会让
# 「这次读的是哪份数据」变成未知数。
CONFIG_NAME = config_module.CONFIG_NAME
CONFIG_ENV = config_module.CONFIG_ENV

# ---- 本项目自有（固定） ----
DATA_ROOT = ROOT / "data"
UPSTREAM_ROOT = DATA_ROOT / "upstream"
STATS_ROOT = DATA_ROOT / "stats"
BUILD_ROOT = ROOT / "build"
KNOWLEDGE_ROOT = DATA_ROOT / "knowledge"
# 统计行的形状契约只有一处：`stats/model.py::STAT_VALUE_SCHEMA`（外加 `WorldStat.validate()`
# 的分层语义），不再维护一份 JSON Schema 副本。

FIXED_ROOTS: dict[str, Path] = {
    "data": DATA_ROOT,
    "upstream": UPSTREAM_ROOT,
    "stats": STATS_ROOT,
    "build": BUILD_ROOT,
    "knowledge": KNOWLEDGE_ROOT,
}

# ---- 上游项目位置（来自 config.yml，两项都必填） ----
_ATTR_KEYS: dict[str, str] = {
    "REF_RECIPE_ANALYSIS": "recipe_analysis",
    "REF_COLLECTOR": "collector",
}
_KEY_ATTRS: dict[str, str] = {key: attr for attr, key in _ATTR_KEYS.items()}

_cache: dict[str, Path] | None = None


def roots() -> dict[str, Path]:
    """配置给出的上游项目目录（2 项，全部必填）。第一次调用时读配置。"""
    global _cache
    if _cache is None:
        _cache = config_module.load()
    return _cache


def _root(key: str) -> Path:
    """按**模块属性**取上游目录。

    走 `getattr` 而不是直接查缓存：测试与工具经常用 `monkeypatch` 覆盖
    `paths.REF_COLLECTOR` 来伪造布局，缓存读不到那个覆盖。
    """
    return getattr(sys.modules[__name__], _KEY_ATTRS[key])


def find_config() -> Path | None:
    return config_module.find_config()


def clear_cache() -> None:
    """丢掉已读的配置。只给测试用。"""
    global _cache
    _cache = None


def __getattr__(name: str):
    key = _ATTR_KEYS.get(name)
    if key is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return roots()[key]


# 解析层名，供 `summary()` 与诊断输出使用。
# `REF` 只出现在 `upstream_*`（`bg data sync` / `verify` 的上游侧定位）。
LOCAL = "local"
REF = "ref"
OVERRIDE = "override"
MISSING = "missing"


@dataclass(frozen=True)
class Resolved:
    """一次路径解析的结果：路径 + 命中了哪一层。"""

    path: Path
    layer: str

    @property
    def local(self) -> bool:
        """是否命中了项目自持的 `data/`（`OVERRIDE` 也算显式指定）。"""
        return self.layer in (LOCAL, OVERRIDE)

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def as_dict(self) -> dict:
        return {"path": str(self.path), "layer": self.layer, "exists": self.exists}


# ------------------------------------------------------------------ 上游产物


def recipe_analysis(pack: str, override: str | None = None) -> Resolved:
    """RecipeAnalysis 的 `output/<pack>/`（graph.json + facts/*.jsonl），由 `bg data sync` 落到 `data/`。"""
    if override:
        return Resolved(Path(override), OVERRIDE)
    path = UPSTREAM_ROOT / pack / "recipe-analysis"
    return Resolved(path, LOCAL if path.exists() else MISSING)


def collector_run(pack: str, override: str | None = None) -> Resolved:
    """WorldGenStatsCollector 的一次统计运行目录（含 summary.json 与各分析器产物）。"""
    if override:
        return Resolved(Path(override), OVERRIDE)
    local = UPSTREAM_ROOT / pack / "worldgen-stats"
    if local.exists():
        # 本地快照可能只有一个 run，也可能是多个时间戳目录的合集。
        runs = sorted(p for p in local.iterdir() if p.is_dir())
        if runs:
            return Resolved(runs[-1], LOCAL)
    return Resolved(local, MISSING)


def template_facts(pack: str, override: str | None = None) -> Resolved:
    """上游 `template.jsonl`（结构模板的箱子构成）。"""
    if override:
        return Resolved(Path(override), OVERRIDE)
    resolved = recipe_analysis(pack)
    if resolved.layer == MISSING:
        return resolved
    return Resolved(resolved.path / "facts" / "template.jsonl", resolved.layer)


def tag_facts(pack: str, override: str | None = None) -> Resolved:
    """上游 `tag.jsonl`（物品 / 方块 / 交易集标签）。战利品表的 `tag` 条目要它才能展开。"""
    if override:
        return Resolved(Path(override), OVERRIDE)
    resolved = recipe_analysis(pack)
    if resolved.layer == MISSING:
        return resolved
    return Resolved(resolved.path / "facts" / "tag.jsonl", resolved.layer)


def mining_facts(pack: str, override: str | None = None) -> Resolved:
    """上游 `facts/mining.jsonl`（方块硬度 + `requires_correct_tool`）。"""
    if override:
        return Resolved(Path(override), OVERRIDE)
    resolved = recipe_analysis(pack)
    if resolved.layer == MISSING:
        return resolved
    return Resolved(resolved.path / "facts" / "mining.jsonl", resolved.layer)


def mining_tool_materials(pack: str, override: str | None = None) -> Resolved:
    """上游 `export/mining_tool_materials.jsonl`（材质 → 挖掘速度，wood 2.0 … diamond 8.0）。"""
    if override:
        return Resolved(Path(override), OVERRIDE)
    resolved = recipe_export(pack)
    if resolved.layer == MISSING:
        return resolved
    return Resolved(resolved.path / "mining_tool_materials.jsonl", resolved.layer)


def recipe_facts(pack: str, override: str | None = None) -> Resolved:
    """上游 `facts/recipe.jsonl`（配方事实：原料与产量）。

    折算比例（9 金粒 = 1 锭、4 粘土球 = 1 块）属于版本事实，
    必须从这里读，不能在案例层硬编码。
    """
    if override:
        return Resolved(Path(override), OVERRIDE)
    resolved = recipe_analysis(pack)
    if resolved.layer == MISSING:
        return resolved
    return Resolved(resolved.path / "facts" / "recipe.jsonl", resolved.layer)


def registry_facts(pack: str, override: str | None = None) -> Resolved:
    """Upstream `facts/registry.jsonl`."""
    if override:
        return Resolved(Path(override), OVERRIDE)
    resolved = recipe_analysis(pack)
    if resolved.layer == MISSING:
        return resolved
    return Resolved(resolved.path / "facts" / "registry.jsonl", resolved.layer)


def worldgen_facts(pack: str, override: str | None = None) -> Resolved:
    """Upstream `facts/worldgen.jsonl`."""
    if override:
        return Resolved(Path(override), OVERRIDE)
    resolved = recipe_analysis(pack)
    if resolved.layer == MISSING:
        return resolved
    return Resolved(resolved.path / "facts" / "worldgen.jsonl", resolved.layer)


def recipe_export(pack: str, override: str | None = None) -> Resolved:
    """上游 `export/` 目录（结构清单、模板表、刷怪表、挖掘表等导出物）。

    2026-11-21 起 RA 把这些从 `output/<pack>/` 根下挪进了 `export/`：
    `structures.jsonl`、`template_containers.jsonl`、`template_blocks.jsonl`、
    `spawn_tables.jsonl`、`structure_containers.jsonl`、`mining_*.{json,jsonl}`。
    """
    if override:
        return Resolved(Path(override), OVERRIDE)
    resolved = recipe_analysis(pack)
    if resolved.layer == MISSING:
        return resolved
    return Resolved(resolved.path / "export", resolved.layer)


def recipe_report(pack: str, override: str | None = None) -> Resolved:
    """上游 `report/` 目录（覆盖率、缺口、可达性，以及 `harvest.jsonl`）。"""
    if override:
        return Resolved(Path(override), OVERRIDE)
    resolved = recipe_analysis(pack)
    if resolved.layer == MISSING:
        return resolved
    return Resolved(resolved.path / "report", resolved.layer)


def harvest_facts(pack: str, override: str | None = None) -> Resolved:
    """上游 `report/harvest.jsonl`（方块 → 最低工具等级 / 最弱合格工具）。

    由 RA 的 IR 从 `minecraft:tool` 组件与 `needs_*_tool` 标签推出，
    下游直接消费，不自己读标签或反推。
    """
    if override:
        return Resolved(Path(override), OVERRIDE)
    resolved = recipe_report(pack)
    if resolved.layer == MISSING:
        return resolved
    return Resolved(resolved.path / "harvest.jsonl", resolved.layer)


def block_transform_facts(pack: str, override: str | None = None) -> Resolved:
    """上游 `facts/block_transform.jsonl`（方块之间的交互式变换）。

    一行 = 「拿着 `transformer` 对方块 `from` 动手，变成 `to`」：
    斧头除锈/脱蜡、锹压平、锄耕地。铜的自然氧化不在里面（那是随机刻，
    不是玩家操作），但**除锈对反向就是氧化对**，下游据此生成氧化链。
    """
    if override:
        return Resolved(Path(override), OVERRIDE)
    resolved = recipe_analysis(pack)
    if resolved.layer == MISSING:
        return resolved
    return Resolved(resolved.path / "facts" / "block_transform.jsonl", resolved.layer)


def serializer_stations(pack: str, override: str | None = None) -> Resolved:
    """上游知识表 `serializer_station.json`（serializer → 工艺设备）。

    它不在 `output/<pack>/` 里，而是 RA 仓库的知识表；`bg data sync` 会复制到
    `data/upstream/<pack>/recipe-knowledge/`。与其它数据一样只读 `data/`。
    """
    if override:
        return Resolved(Path(override), OVERRIDE)
    local = UPSTREAM_ROOT / pack / "recipe-knowledge" / "serializer_station.json"
    return Resolved(local, LOCAL if local.exists() else MISSING)


# ------------------------------------------------------------------ 数据包（合并后）


def datapack(pack: str, override: str | None = None) -> Resolved:
    """本项目实际消费的**数据包视图**：原版 ⊗ 地形包 overlay 的合并结果。

    它就是「生效后的 `data/`」，不是纯原版——地形包覆盖已经叠进去了。
    由上游 `inputs/<pack>/datapack-merged/data/` 整棵同步而来，另外补上上游
    合并包没导的 `minecraft/structure`（结构模板来自 datagen 缓存）。

    不叫 `vanilla` 是因为名字会撒谎：只要地形包将来覆盖了别的类别，
    「原版」这个说法就站不住；叫 `datapack` 则永远成立。
    """
    if override:
        return Resolved(Path(override), OVERRIDE)
    local = UPSTREAM_ROOT / pack / "datapack" / "data"
    return Resolved(local, LOCAL if local.is_dir() else MISSING)


def _versioned_dir(parent: Path, current: str, legacy: str) -> Path:
    """`parent/current`, or `parent/legacy` when only the legacy directory exists."""
    if (parent / current).is_dir() or not (parent / legacy).is_dir():
        return parent / current
    return parent / legacy


def loot_tables(pack: str, override: str | None = None) -> Resolved:
    """战利品表：数据包里的 `minecraft/loot_table`。

    没有合并产物时**不回退到未合并的原版表**：那样会把地形包覆盖悄悄丢掉，
    算出来的期望值看起来正常但是错的。
    """
    if override:
        return Resolved(Path(override), OVERRIDE)
    resolved = datapack(pack)
    path = _versioned_dir(resolved.path / "minecraft", "loot_table", "loot_tables")
    if resolved.layer != MISSING and path.is_dir():
        return Resolved(path, resolved.layer)
    return Resolved(path, MISSING)


def structure_templates(pack: str, override: str | None = None) -> Resolved:
    """结构模板（`.snbt`）：数据包里的 `minecraft/structure`。"""
    if override:
        return Resolved(Path(override), OVERRIDE)
    resolved = datapack(pack)
    path = _versioned_dir(resolved.path / "minecraft", "structure", "structures")
    if resolved.layer != MISSING and path.is_dir():
        return Resolved(path, resolved.layer)
    return Resolved(path, MISSING)


# ------------------------------------------------------------------ 上游源位置
#
# 下面这组只回答「上游在哪」，不参与优先级回退。存在的理由：同步的源必须来自上游，
# 不能来自本地已有快照——否则 `bg data sync` 会把自己上次的复制当成源，
# 重同步退化成空操作，上游更新永远进不来。
#
# 解析看 `recipe_analysis()` 等（优先级：data/ > 上游），同步看这里（只认上游）。


def upstream_recipe_analysis(pack: str) -> Resolved:
    """上游 RecipeAnalysis 的 `output/<pack>/`。"""
    path = _root("recipe_analysis") / "output" / pack
    return Resolved(path, REF if path.exists() else MISSING)


def upstream_recipe_vocabulary() -> Resolved:
    """上游知识表目录 `knowledge/maps/vocabulary/`（同步的源，只认 ref）。

    含 `serializer_station.json`：配方 IR 的「设备」语义在这里定义。
    """
    path = _root("recipe_analysis") / "knowledge" / "maps" / "vocabulary"
    return Resolved(path, REF if path.is_dir() else MISSING)


def upstream_collector_run(pack: str) -> Resolved:
    """Latest WGS stats run for this version (runs without a recorded version are kept)."""
    stats = _root("collector") / "stats"
    if stats.is_dir():
        runs = sorted(
            p for p in stats.iterdir() if p.is_dir() and _run_version(p) in (None, pack)
        )
        if runs:
            return Resolved(runs[-1], REF)
    return Resolved(stats, MISSING)


def _run_version(run: Path) -> str | None:
    try:
        summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    target = summary.get("target") if isinstance(summary, dict) else None
    version = target.get("minecraftVersion") if isinstance(target, dict) else None
    return version if isinstance(version, str) else None


def upstream_datapack_merged(pack: str) -> Resolved:
    """上游合并后的数据包：原版 ⊗ 地形包 overlay 的**生效结果**。

    RecipeAnalysis 产出 `inputs/<pack>/datapack-merged/data/`——它已经按
    `pack.mcmeta` 的区间挑出适用 overlay 并逐层覆盖。下游直接取这份 `data/`，
    不再自己判断 overlay、不再自己叠加，避免同一套合并语义有两份实现。
    """
    path = _root("recipe_analysis") / "inputs" / pack / "datapack-merged" / "data"
    return Resolved(path, REF if path.exists() else MISSING)


def upstream_vanilla_structures(pack: str) -> Resolved:
    """上游解包出的原版结构模板（SNBT）。

    结构形状（门框、房间、矿道）只能从模板里的**方块坐标**读，而 `facts/` 只
    导出「每模板多少块某方块」的计数。所以形状匹配需要这一份原件。

    它在 RA 的服务器缓存下（`server-<hash>/structures/data`），目录名带 hash，
    所以用 glob 发现最新的一个；找不到就报 MISSING，不静默退化。
    """
    cache = _root("recipe_analysis") / ".cache"
    if cache.is_dir():
        # Cache directory names do not encode the version; check the bundler instead.
        for server in sorted(cache.glob("server-*"), reverse=True):
            if not (server / "bundler" / "META-INF" / "versions" / pack).is_dir():
                continue
            path = server / "structures" / "data"
            if any((path / "minecraft" / name).is_dir() for name in ("structure", "structures")):
                return Resolved(path, REF)
    return Resolved(cache, MISSING)


# ------------------------------------------------------------------ 诊断


def summary(pack: str) -> dict:
    """当前解析状态，供 `bg doctor` 展示。"""
    entries = {
        "recipe_analysis": recipe_analysis(pack),
        "collector_run": collector_run(pack),
        "datapack": datapack(pack),
    }
    return {
        "pack": pack,
        "fully_local": all(item.local for item in entries.values()),
        "sources": {name: item.as_dict() for name, item in entries.items()},
    }
