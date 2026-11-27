"""共享模型与模块级工具：错误类型、来源记录、斑块、暴露份额、门框变体等。"""

from __future__ import annotations

import json
import math

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping
from bingo_grading import paths
from bingo_grading.gamedata import FISH_WATER_BIOMES, LAVA_LAKE_SETS, WATER_BLOCKS
from bingo_grading.sources.knowledge import StructureMaps, load_structure_maps


class CaseError(ValueError):
    """案例所需的数据缺失或形态不对。"""




@dataclass
class Sourced:
    """一个数值 + 它的出处。"""

    value: float
    source: str
    detail: str = ""

    def as_dict(self) -> dict:
        return {"value": round(self.value, 4), "source": self.source, "detail": self.detail}




@dataclass(frozen=True)
class PatchSource:
    """地下成片资源的一个纵段：一条独立的「去那儿」来源。"""

    subject: str
    y_range: tuple[int, int]
    per_chunk: float
    share: float
    depth: int
    horizontal: float
    shaft_tu: float
    travel_tu: float
    cost_tu: float
    blocks_per_patch: float
    detail: str
    # 人读的地点描述：「下挖到 y47，走到第一片岩浆」/「跑图 215 格到最近的岩浆湖」。
    # 调用方拼 label 时用它，免得自己按 `y_range` 组装（两种口径字段含义不同）。
    where: str = ""
    # `shaft`（按纵段挖下去）或 `lake`（按实测的湖间距跑过去）。
    kind: str = "shaft"

    @property
    def key(self) -> str:
        """同一资源不同来源的动作需要一个稳定的后缀。"""
        if self.kind == "lake":
            return "lake"
        return f"y{self.y_range[0]}"

    def capacity_batches(self, batch: float) -> float:
        """这一片能支撑几批。"""
        return self.blocks_per_patch / batch if batch > 0 else 0.0

    def as_dict(self) -> dict:
        return {
            "subject": self.subject,
            "y_range": list(self.y_range),
            "per_chunk": round(self.per_chunk, 4),
            "depth": self.depth,
            "cost_tu": round(self.cost_tu, 4),
            "blocks_per_patch": self.blocks_per_patch,
            "where": self.where,
            "kind": self.kind,
        }




def _find_distance(density: float) -> float:
    """按实测密度反推「找到首片要走多远」（格）。

    `density` 是每区块可捡块数。把方块看成二维泊松点过程，
    空间密度 `λ = density / 256`（块/格²），最近邻平均距离 `0.5/√λ`，
    代回去就是 `8/√density` 格。

    以前这里是常量 100 格，对所有方块一视同仁——燧石（62 块/区块可捡）
    被罚 2.56 TU 的找路费，黑曜石（0.027 块/区块）反而只罚 100 格。
    密度差 2000 倍的方块给同一个数，等于把便宜的抬贵、把贵的说便宜。

    下限 4 格（就在脚下，没必要再算）、上限 512 格（跨半张图，再多
    也不该由「密度」外推，那种方块本来就不该有地表来源）。
    """
    if density <= 0:
        return 512.0
    return min(512.0, max(4.0, 8.0 / math.sqrt(density)))




@lru_cache(maxsize=4)
def _harvest_rows(path: str) -> dict[str, dict]:
    """上游 `harvest.jsonl` → `{方块: 行}`。坏行跳过（由调用方按缺失处理）。"""
    rows: dict[str, dict] = {}
    target = Path(path)
    if not target.is_file():
        return rows
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        block = row.get("block")
        if isinstance(block, str):
            rows[block] = row
    return rows


@lru_cache(maxsize=4)
def _registry_entries(path: str, registry: str) -> frozenset[str] | None:
    """Entries of one registry in upstream `registry.jsonl`; `None` if absent."""
    target = Path(path)
    if not target.is_file():
        return None
    for line in target.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line).get("payload") or {}
        except json.JSONDecodeError:
            continue
        if payload.get("registry") == registry:
            return frozenset(str(entry) for entry in payload.get("entries") or ())
    return None


@lru_cache(maxsize=4)
def _code_container_rows(path: str) -> tuple[dict, ...]:
    """Upstream `export/structure_containers.jsonl`: containers placed by structure code."""
    target = Path(path)
    if not target.is_file():
        return ()
    rows: list[dict] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return tuple(rows)


@lru_cache(maxsize=4)
def _biome_feature_blocks(worldgen: str, biome_dir: str) -> frozenset[str]:
    """Blocks placed by features reachable from biome `features` lists.

    Only `block` references on feature rows count; blocks on placed feature rows
    come from placement predicates.
    """
    nodes: dict[tuple[str, str], list[dict]] = {}
    target = Path(worldgen)
    if not target.is_file():
        return frozenset()
    for line in target.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line).get("payload") or {}
        except json.JSONDecodeError:
            continue
        kind, node_id = payload.get("worldgen_kind"), payload.get("id")
        if not isinstance(kind, str) or not isinstance(node_id, str):
            continue
        marker = f"/worldgen/{kind}/"
        if marker not in node_id:
            continue
        name = "minecraft:" + node_id.split(marker, 1)[1]
        kind = "feature" if kind == "configured_feature" else kind
        nodes[(kind, name)] = list(payload.get("references") or [])
    stack: list[tuple[str, str]] = []
    for path in sorted(Path(biome_dir).glob("*.json")):
        try:
            biome = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for step in biome.get("features") or ():
            if isinstance(step, list):
                stack.extend(("placed_feature", ref) for ref in step if isinstance(ref, str))
    blocks: set[str] = set()
    seen: set[tuple[str, str]] = set()
    while stack:
        node = stack.pop()
        if node in seen or node not in nodes:
            continue
        seen.add(node)
        for ref in nodes[node]:
            ref_id, slot = ref.get("id"), ref.get("slot")
            if not isinstance(ref_id, str):
                continue
            if slot == "block" and node[0] == "feature":
                blocks.add(ref_id)
            elif slot == "feature":
                # Nested features may reference either a feature or a placed feature.
                stack.append(("feature", ref_id))
                stack.append(("placed_feature", ref_id))
    return frozenset(blocks)




@dataclass(frozen=True)
class BlockExposure:
    """一个方块的四分暴露（来自上游 6 面邻接判定，分母是方块个数）。"""

    per_chunk: float
    sky: float  # 贴天空空气（露在地表）
    cave: float  # 贴洞穴空气（要下去，但不用盲挖）
    water: float  # 贴水（水下露头）
    buried: float  # 四面都不通

    @property
    def walkable(self) -> float:
        """走过去/游过去就能拿到的比例。"""
        return self.sky + self.water

    @property
    def reachable(self) -> float:
        """能走到就要的比例（含洞穴）。"""
        return self.walkable + self.cave




def _with_wall_variants(
    block: str, aliases: Mapping[str, tuple[str, ...]] | None = None
) -> tuple[str, ...]:
    """方块名连同它的挂墙变体：`torch` → `wall_torch`，`dragon_head` → `dragon_wall_head`。

    结构里的火把 / 告示牌 / 头颅几乎都以挂墙形态生成（末地船船头挂的是
    `dragon_wall_head`），而物品来自挖掉它。只按本体名字查
    `structure_content.blocks_per_start` 会一个也找不到。

    `aliases` 来自上游 `code_link.jsonl` 的 `loot_alias`（47 条）：它们记的是
    游戏里的 `dropsLike` 关系——挂墙变体没有自己的战利品表，直接读本体那张。
    所以「结构放了 wall_torch」与「shatter 它掉 torch」两头都成立，中间只差
    一个名字映射，就是这里。

    上游数据缺时退回按 MC 命名约定拼（只能盖头颅那一类），不因为没同步
    就丢掉整条路线。
    """
    names = [block]
    if aliases:
        names.extend(aliases.get(block, ()))
    namespace, _, rest = block.partition(":")
    for suffix, wall in (("_head", "_wall_head"), ("_skull", "_wall_skull")):
        if rest.endswith(suffix):
            names.append(f"{namespace}:{rest[: -len(suffix)]}{wall}")
            break
    return tuple(dict.fromkeys(names))




def _normalize_condition(condition: str) -> str:
    """把 tools=present / tools=tools=present 归一，兼容两种写法。

    同一 `subject` 有多张表时（击杀与剪毛都是 `minecraft:sheep`），存储侧会在
    `condition` 后补 `|trigger=kill` 这类后缀作为主键区分（见 `importers/loot.py`
    的 `build_drops`）。工具条件只是后半段，比较前先剥掉。
    """
    text = condition.strip().split("|", 1)[0]
    while text.startswith("tools=") and text.count("=") > 1 and "=tools=" in text:
        text = text[len("tools=") :]
    return text




@lru_cache(maxsize=1)
def _structure_maps() -> StructureMaps:
    """结构集合映射（集合 → 变体）。读一次就缓存。"""
    return load_structure_maps(paths.KNOWLEDGE_ROOT / "structure_sets.toml")
