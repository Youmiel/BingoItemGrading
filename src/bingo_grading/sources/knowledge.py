"""知识表：路线分类与结构映射。

两者都是表驱动的数据（`data/knowledge/*.toml`）：按名字查、条数多、加载时统一校验，
所以留在数据文件里。概率只有几条纯数值，是**游戏数据**，归 `gamedata.PROBABILITY_ROWS`
（本模块 re-export 给来源索引层用）。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..gamedata import PROBABILITY_ROWS


class KnowledgeError(ValueError):
    """知识表不合法。"""


@dataclass(frozen=True)
class Probability:
    item: str
    source_type: str
    subject: str
    probability: float
    basis: str


@dataclass
class Probabilities:
    """`(item, source_type, subject)` → 概率。

    未登记的来源按确定性处理，但这不是断言「它确定」——覆盖率报告会把
    未登记的低覆盖物品列出来，供逐轮补表。
    """

    table: dict[tuple[str, str, str], Probability] = field(default_factory=dict)

    def lookup(self, item: str, source_type: str, subject: str) -> Probability | None:
        return self.table.get((item, source_type, subject))

    def __len__(self) -> int:
        return len(self.table)


@dataclass(frozen=True)
class RouteRules:
    """路线判定的知识集合。未命中一律按主世界地表处理。"""

    underground_block_prefixes: tuple[str, ...] = ()
    underground_blocks: frozenset[str] = frozenset()
    underground_structure_prefixes: tuple[str, ...] = ()
    dimension_keywords: tuple[tuple[str, tuple[str, ...]], ...] = ()
    structure_dimensions: tuple[tuple[str, str], ...] = ()
    underground_structure_sets: frozenset[str] = frozenset()

    def is_underground_block(self, block: str) -> bool:
        if block in self.underground_blocks:
            return True
        return any(block.startswith(prefix) for prefix in self.underground_block_prefixes)

    def is_underground_structure(self, table: str) -> bool:
        return any(table.startswith(prefix) for prefix in self.underground_structure_prefixes)

    def other_dimension_of(self, subject: str) -> str | None:
        """来源对象所属的非主世界维度；主世界返回 `None`。"""
        for dimension, keywords in self.dimension_keywords:
            if any(keyword in subject for keyword in keywords):
                return dimension
        return None

    def dimension_of_structures(self, prereq: tuple[str, ...]) -> str | None:
        """前置里的结构节点 → 维度。比关键词可靠：结构有明确的维度归属。"""
        table = dict(self.structure_dimensions)
        for node in prereq:
            if not node.startswith("structure:"):
                continue
            name = node.split(":", 1)[1]
            dimension = table.get(name)
            if dimension is not None:
                return dimension
        return None


@dataclass(frozen=True)
class StructureMaps:
    """三套映射，都只用于把来源与统计对齐，不参与难度计算。"""

    loot_prefixes: tuple[tuple[str, str], ...] = ()
    template_prefixes: tuple[tuple[str, str], ...] = ()
    underground_sets: frozenset[str] = frozenset()
    # 结构集合 → 具体变体（`minecraft:villages` → `village_plains`, ...）。
    # 密度按集合测、箱数按变体测，两边要靠它对齐。
    members: tuple[tuple[str, tuple[str, ...]], ...] = ()
    # 没有包围盒的结构的每处占地体积（格³）。仅地牢这类 feature 需要：
    # 搜索代价用体积占比，分母要它。
    feature_volumes: tuple[tuple[str, float], ...] = ()
    # 结构顶层的 y。地下结构必须先竖下去，`ctx.travel` 只算跑图，不含这一段。
    structure_depths: tuple[tuple[str, float], ...] = ()
    # 结构集合 → 指向它的藏宝图物品。知道位置不是白送的，图要跑一趟去拿。
    maps: tuple[tuple[str, str], ...] = ()

    def set_for_table(self, table: str) -> str | None:
        """最长前缀匹配。表路径去掉命名空间后与键比较。"""
        _, _, rest = table.partition(":")
        return _longest_prefix(rest, self.loot_prefixes)

    def set_for_template(self, path: str) -> str | None:
        """模板路径 → 结构集合。键是 `structure/` 之后的部分。"""
        normalized = path.replace("data/", "", 1)
        rest = normalized.split("/structure/", 1)[-1]
        return _longest_prefix(rest, self.template_prefixes)

    def members_of(self, structure_set: str) -> tuple[str, ...]:
        """某集合有哪些具体变体。没登记返回空元组。"""
        for key, value in self.members:
            if key == structure_set:
                return value
        return ()

    def set_of(self, variant: str) -> str | None:
        """某个变体属于哪个结构集。反查 `members`。

        `structure_density` 只按**集合**记录（`minecraft:ancient_cities`），
        但模板绑定这类来源给的是**变体**（`minecraft:ancient_city`）。
        不做这层反查，`nearest_distance(变体)` 会返回 `None`，行程成本
        静默归零——ancient_city 的三个绑定物品因此曾是「免费到达」。
        """
        for key, value in self.members:
            if variant in value:
                return key
        return None

    def volume_of(self, structure_set: str) -> float | None:
        """没有包围盒的结构每处占多体积。没登记返回 `None`。"""
        for key, value in self.feature_volumes:
            if key == structure_set:
                return value
        return None

    def depth_of(self, structure_set: str) -> float | None:
        """结构顶层的 y。没登记返回 `None`（调用方应把「未含竖井」记进假设）。"""
        for key, value in self.structure_depths:
            if key == structure_set:
                return value
        return None

    def map_for(self, structure_set: str) -> str | None:
        """指向该结构集合的藏宝图物品。没登记返回 `None`（不靠地图定位）。"""
        for key, value in self.maps:
            if key == structure_set:
                return value
        return None


def probabilities_table() -> Probabilities:
    """内置概率表。"""
    table = {
        (item, source_type, subject): Probability(
            item=item,
            source_type=source_type,
            subject=subject,
            probability=probability,
            basis=basis,
        )
        for item, source_type, subject, probability, basis in PROBABILITY_ROWS
    }
    return Probabilities(table=table)


def load_routes(path: Path) -> RouteRules:
    doc = _read_toml(path)
    prefixes = _str_tuple(doc, path, "underground_block_prefixes")
    blocks = _str_tuple(doc, path, "underground_blocks")
    structures = _str_tuple(doc, path, "underground_structure_prefixes")

    keywords_raw = doc.get("dimension_keywords", {})
    if not isinstance(keywords_raw, dict):
        raise KnowledgeError(f"{path}: dimension_keywords 必须是表")
    keywords: list[tuple[str, tuple[str, ...]]] = []
    for dimension, values in keywords_raw.items():
        # 顶层键写在 `[dimension_keywords]` 之后会被 TOML 归进该表。
        # 不拦下这种写法，维度名会变成 `underground_blocks` 一类的键。
        if not dimension.startswith(("the_", "minecraft:")):
            raise KnowledgeError(
                f"{path}: dimension_keywords 里出现可疑的维度名 {dimension!r}；"
                "顶层键必须写在 [dimension_keywords] 之前"
            )
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise KnowledgeError(f"{path}: dimension_keywords.{dimension} 必须是字符串数组")
        keywords.append((dimension, tuple(values)))

    sets = _str_tuple(doc, path, "underground_structure_sets")

    structure_dims_raw = doc.get("structure_dimensions") or {}
    if not isinstance(structure_dims_raw, dict):
        raise KnowledgeError(f"{path}: structure_dimensions 必须是表")
    structure_dims: list[tuple[str, str]] = []
    for name, dimension in structure_dims_raw.items():
        if not isinstance(dimension, str) or not dimension:
            raise KnowledgeError(f"{path}: structure_dimensions.{name} 必须是维度 ID")
        structure_dims.append((name, dimension))

    return RouteRules(
        underground_block_prefixes=prefixes,
        underground_blocks=frozenset(blocks),
        underground_structure_prefixes=structures,
        dimension_keywords=tuple(keywords),
        structure_dimensions=tuple(structure_dims),
        underground_structure_sets=frozenset(sets),
    )


def _longest_prefix(value: str, pairs: tuple[tuple[str, str], ...]) -> str | None:
    best: tuple[int, str] | None = None
    for prefix, set_id in pairs:
        if value.startswith(prefix) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), set_id)
    return best[1] if best else None


def load_structure_maps(path: Path) -> StructureMaps:
    """读战利品表 → 结构集合的映射。文件缺失时返回空映射，不报错。"""
    path = Path(path)
    if not path.exists():
        return StructureMaps()
    doc = _read_toml(path)
    raw = doc.get("loot_prefixes")
    if not isinstance(raw, dict):
        raise KnowledgeError(f"{path}: 缺 [loot_prefixes] 表")
    pairs: list[tuple[str, str]] = []
    for prefix, set_id in raw.items():
        if not isinstance(set_id, str) or not set_id:
            raise KnowledgeError(f"{path}: loot_prefixes.{prefix} 必须是结构集合 ID")
        if prefix.startswith("minecraft:"):
            raise KnowledgeError(
                f"{path}: loot_prefixes 的键不带命名空间（{prefix!r}）；"
                "匹配前已去掉表的命名空间"
            )
        pairs.append((prefix, set_id))

    templates_raw = doc.get("template_prefixes") or {}
    if not isinstance(templates_raw, dict):
        raise KnowledgeError(f"{path}: template_prefixes 必须是表")
    template_pairs: list[tuple[str, str]] = []
    for prefix, set_id in templates_raw.items():
        if not isinstance(set_id, str) or not set_id:
            raise KnowledgeError(f"{path}: template_prefixes.{prefix} 必须是结构集合 ID")
        template_pairs.append((prefix, set_id))

    underground = _str_tuple(doc, path, "underground_sets")

    members_raw = doc.get("structure_members") or {}
    if not isinstance(members_raw, dict):
        raise KnowledgeError(f"{path}: structure_members 必须是表")
    members: list[tuple[str, tuple[str, ...]]] = []
    for set_id, value in members_raw.items():
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise KnowledgeError(
                f"{path}: structure_members.{set_id} 必须是字符串数组"
            )
        members.append((set_id, tuple(value)))

    volumes_raw = doc.get("feature_volumes") or {}
    if not isinstance(volumes_raw, dict):
        raise KnowledgeError(f"{path}: feature_volumes 必须是表")
    feature_volumes: list[tuple[str, float]] = []
    for set_id, value in volumes_raw.items():
        if not isinstance(value, (int, float)) or value <= 0:
            raise KnowledgeError(
                f"{path}: feature_volumes.{set_id} 必须是正数（格³）"
            )
        feature_volumes.append((set_id, float(value)))

    depths_raw = doc.get("structure_depths") or {}
    if not isinstance(depths_raw, dict):
        raise KnowledgeError(f"{path}: structure_depths 必须是表")
    depths: list[tuple[str, float]] = []
    for set_id, value in depths_raw.items():
        if not isinstance(value, (int, float)):
            raise KnowledgeError(
                f"{path}: structure_depths.{set_id} 必须是数字（顶层 y）"
            )
        depths.append((set_id, float(value)))

    maps_raw = doc.get("structure_maps") or {}
    if not isinstance(maps_raw, dict):
        raise KnowledgeError(f"{path}: structure_maps 必须是表")
    maps: list[tuple[str, str]] = []
    for set_id, value in maps_raw.items():
        if not isinstance(value, str) or not value:
            raise KnowledgeError(
                f"{path}: structure_maps.{set_id} 必须是物品 ID"
            )
        maps.append((set_id, value))

    return StructureMaps(
        loot_prefixes=tuple(sorted(pairs, key=lambda item: -len(item[0]))),
        template_prefixes=tuple(sorted(template_pairs, key=lambda item: -len(item[0]))),
        underground_sets=frozenset(underground),
        members=tuple(members),
        feature_volumes=tuple(feature_volumes),
        structure_depths=tuple(depths),
        maps=tuple(maps),
    )


def _read_toml(path: Path) -> dict:
    path = Path(path)
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        raise KnowledgeError(f"知识表不存在：{path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise KnowledgeError(f"{path} 不是合法 TOML：{exc}") from exc


def _str_tuple(doc: dict, path: Path, key: str) -> tuple[str, ...]:
    value = doc.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise KnowledgeError(f"{path}: {key} 必须是字符串数组")
    return tuple(value)
