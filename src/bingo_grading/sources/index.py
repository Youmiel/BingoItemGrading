"""把上游图产物导出成来源路径索引。

索引是「每条来源一行」：一个物品有几条独立获取途径，就有几行。
合成配方不产生行，合成链由展开器递归处理。

产物只读上游、写本项目的 `build/`，缺上游时只有槽位枚举受影响。
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .classify import SYNTHETIC_FAMILIES, classify, subject_of
from .closure import expand
from .constants import INDEX_SCHEMA, INDEX_VERSION
from .drops import DropIndex
from .exposure import ExposureRoutes
from .graph import Graph
from .knowledge import Probabilities, RouteRules, StructureMaps
from .placement import PlacementIndex
from .routes import decide


@dataclass(frozen=True)
class SourceRecord:
    item: str
    source_type: str
    subject: str
    route: str
    prereq: tuple[str, ...]
    biomes: tuple[str, ...]
    notes: tuple[str, ...]
    cycles: tuple[str, ...]
    probabilistic: bool
    probability: float | None
    is_reverse: bool
    primary_source_hint: str | None
    rule_ref: str
    family: str
    truncated: bool
    # 查世界统计用的键。战利品表来源是结构集合（`minecraft:villages`），
    # 其余来源与 `subject` 相同。未映射时为 `None`，由缺口报告列出。
    stat_subject: str | None = None
    structure_set: str | None = None
    # 每次触发（破坏/击杀/开箱/剪毛/刷扫）的期望产出件数。来自战利品表求值，
    # 不是手工概率。`None` 表示统计层没有这项数据。
    yield_per_trigger: float | None = None
    # 这个数对应的工具条件（`tools=present` / `tools=absent`）。
    yield_condition: str | None = None
    # 这个数的出处：`loot_expectation:<subject>` 或 `builtin_probabilities`（人工兼容层）。
    yield_source: str | None = None

    def as_dict(self) -> dict:
        return {
            "index_schema": INDEX_SCHEMA,
            "item": self.item,
            "source_type": self.source_type,
            "subject": self.subject,
            "stat_subject": self.stat_subject,
            "structure_set": self.structure_set,
            "yield_per_trigger": self.yield_per_trigger,
            "yield_condition": self.yield_condition,
            "yield_source": self.yield_source,
            "route": self.route,
            "hard_prereq": list(self.prereq),
            "biomes": list(self.biomes),
            "notes": list(self.notes),
            "cycles": list(self.cycles),
            "probabilistic": self.probabilistic,
            "probability": self.probability,
            "is_reverse": self.is_reverse,
            "primary_source_hint": self.primary_source_hint,
            "rule_ref": self.rule_ref,
            "family": self.family,
            "truncated": self.truncated,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> SourceRecord:
        return cls(
            item=raw["item"],
            source_type=raw["source_type"],
            subject=raw["subject"],
            route=raw["route"],
            prereq=tuple(raw.get("hard_prereq") or ()),
            biomes=tuple(raw.get("biomes") or ()),
            notes=tuple(raw.get("notes") or ()),
            cycles=tuple(raw.get("cycles") or ()),
            probabilistic=bool(raw.get("probabilistic", False)),
            probability=raw.get("probability"),
            is_reverse=bool(raw.get("is_reverse", False)),
            primary_source_hint=raw.get("primary_source_hint"),
            rule_ref=raw["rule_ref"],
            family=raw.get("family", ""),
            truncated=bool(raw.get("truncated", False)),
            stat_subject=raw.get("stat_subject"),
            structure_set=raw.get("structure_set"),
            yield_per_trigger=raw.get("yield_per_trigger"),
            yield_condition=raw.get("yield_condition"),
            yield_source=raw.get("yield_source"),
        )


@dataclass
class BuildResult:
    label: str
    records: list[SourceRecord] = field(default_factory=list)
    # 图里有 `item:` 节点、却一条来源行都没产出的物品。纯配方物品是正常的
    # （`recipes` 族本就不算来源），其余说明来源在建索引时被过滤掉了。
    graph_items: int = 0
    no_source_items: list[str] = field(default_factory=list)
    craft_only_items: list[str] = field(default_factory=list)
    unmapped_family: Counter = field(default_factory=Counter)
    unreachable_items: list[str] = field(default_factory=list)
    truncated: int = 0
    cycled: Counter = field(default_factory=Counter)
    by_type: Counter = field(default_factory=Counter)
    by_route: Counter = field(default_factory=Counter)
    # 概率从哪里来：`loot_expectation:<subject>` / `builtin_probabilities` / `none`。
    by_yield_source: Counter = field(default_factory=Counter)
    unmapped_struct: Counter = field(default_factory=Counter)
    # 只由结构模板放置的 block_drop 来源数，用于观察修复覆盖面。
    structure_only: int = 0

    @property
    def filtered_items(self) -> list[str]:
        """图里有节点、却不是只由配方构成、仍没产出来源行的物品。"""
        craft = set(self.craft_only_items)
        return [item for item in self.no_source_items if item not in craft]

    def write(self, path: Path) -> None:
        """按物品、来源类别、规则 ID 排序写出，两次构建逐字节一致。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        header = {
            "index_version": INDEX_VERSION,
            "index_schema": INDEX_SCHEMA,
            "label": self.label,
            "source_count": len(self.records),
            "item_count": len({record.item for record in self.records}),
            "graph_items": self.graph_items,
            "no_source_items": len(self.no_source_items),
            "craft_only_items": len(self.craft_only_items),
            "filtered_items": len(self.filtered_items),
            "no_source_samples": self.no_source_items[:10],
            "filtered_samples": self.filtered_items[:10],
            "unmapped_family": dict(self.unmapped_family),
            "truncated": self.truncated,
            "cycled": dict(self.cycled),
        }
        lines = [json.dumps(header, ensure_ascii=False, sort_keys=True)]
        for record in sorted(
            self.records, key=lambda item: (item.item, item.source_type, item.rule_ref)
        ):
            lines.append(json.dumps(record.as_dict(), ensure_ascii=False, sort_keys=True))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    def summary(self) -> dict:
        return {
            "label": self.label,
            "items": len({record.item for record in self.records}),
            "sources": len(self.records),
            "graph_items": self.graph_items,
            "no_source_items": len(self.no_source_items),
            "no_source_samples": self.no_source_items[:10],
            "craft_only_items": len(self.craft_only_items),
            "filtered_items": len(self.filtered_items),
            "filtered_samples": self.filtered_items[:10],
            "by_type": dict(self.by_type.most_common()),
            "by_route": dict(self.by_route.most_common()),
            "by_yield_source": dict(self.by_yield_source.most_common()),
            "unmapped_family": dict(self.unmapped_family.most_common()),
            "unmapped_structure": dict(self.unmapped_struct.most_common(10)),
            "unmapped_structure_total": sum(self.unmapped_struct.values()),
            "structure_only_sources": self.structure_only,
            "truncated": self.truncated,
            "cycled_total": sum(self.cycled.values()),
            "cycled_top": dict(self.cycled.most_common(5)),
            "unreachable_items": self.unreachable_items[:20],
            "unreachable_count": len(self.unreachable_items),
        }


def build(
    graph: Graph,
    probabilities: Probabilities,
    routes: RouteRules,
    structures: StructureMaps | None = None,
    placement: PlacementIndex | None = None,
    drops: DropIndex | None = None,
    exposure: ExposureRoutes | None = None,
) -> BuildResult:
    result = BuildResult(label=graph.label)
    structures = structures or StructureMaps()
    placement = placement or PlacementIndex.from_graph(graph)
    heads = sorted(graph.item_heads())
    result.graph_items = len(heads)
    for node in heads:
        item = node.split(":", 1)[1]
        if not graph.is_true(node):
            result.unreachable_items.append(item)
            continue
        before = len(result.records)
        for rule in graph.rules_for(node):
            source_type = classify(rule)
            if source_type is None:
                # `recipes` 是故意不产生来源行的，不算未映射。
                if rule.family and rule.family not in SYNTHETIC_FAMILIES:
                    result.unmapped_family[rule.family] += 1
                continue

            closure = expand(graph, rule.body)
            if closure.truncated:
                result.truncated += 1
            for cycle in closure.cycles:
                result.cycled[cycle] += 1

            subject = subject_of(rule, source_type)
            structure_set: str | None = None
            if source_type in ("mob_drop", "shear"):
                # 剪切与击杀的对象是实体本身：`minecraft:sheep/black` 的密度
                # 与白羊毛同为 `minecraft:sheep`。
                stat_subject: str | None = base_entity(subject)
            elif source_type in ("loot_table", "suspicious_block"):
                # 箱子与考古点按结构集合测量，不按表路径。
                # 必须用完整表路径匹配：`subject` 已降成裸名
                # （`archaeology/trail_ruins_common` → `minecraft:trail_ruins_common`），
                # 拿它匹配会丢掉类别前缀，一条也命中不了。
                table_id = rule.meta.get("table")
                key = table_id if isinstance(table_id, str) else subject
                structure_set = structures.set_for_table(key)
                stat_subject = structure_set
            else:
                stat_subject = subject

            if stat_subject is None:
                result.unmapped_struct[subject] += 1

            probability = probabilities.lookup(item, source_type, subject)

            # 优先用战利品表算出来的期望；没有这项统计时才退回手工表。
            derived = drops.yield_for(item, source_type, subject) if drops is not None else None
            if derived is not None:
                yield_per_trigger: float | None = float(derived[0])
                yield_condition: str | None = derived[1]
                yield_source: str | None = f"loot_expectation:{subject}"
                probabilistic = yield_per_trigger < 1.0
                probability_value: float | None = None
            elif probability is not None:
                yield_per_trigger = None
                yield_condition = None
                yield_source = "builtin_probabilities"
                probabilistic = probability.probability < 1.0
                probability_value = probability.probability
            else:
                yield_per_trigger = None
                yield_condition = None
                yield_source = None
                probabilistic = False
                probability_value = None
            result.by_yield_source[yield_source or "none"] += 1

            # 方块只在结构里出现时，路线要看结构，不能看战利品表目录名。
            block_sets: tuple[str, ...] = ()
            structure_only = False
            if source_type == "block_drop" and placement is not None:
                block_fact = placement.of(subject)
                if block_fact is not None and block_fact.structure_only:
                    structure_only = True
                    block_sets = placement.structure_sets(subject, structures)
                    result.structure_only += 1

            route = decide(
                source_type=source_type,
                subject=subject,
                prereq=closure.prereq,
                rules=routes,
                structure_set=structure_set,
                block_sets=block_sets,
                structure_only=structure_only,
                exposure=exposure,
            )
            record = SourceRecord(
                item=item,
                source_type=source_type,
                subject=subject,
                route=route,
                prereq=closure.prereq,
                biomes=closure.biomes,
                notes=closure.notes,
                cycles=closure.cycles,
                probabilistic=probabilistic,
                probability=probability_value,
                is_reverse=False,
                primary_source_hint=None,
                rule_ref=rule.id,
                family=rule.family,
                truncated=closure.truncated,
                stat_subject=stat_subject,
                structure_set=structure_set,
                yield_per_trigger=yield_per_trigger,
                yield_condition=yield_condition,
                yield_source=yield_source,
            )
            result.records.append(record)
            result.by_type[source_type] += 1
            result.by_route[route] += 1
        if len(result.records) == before:
            result.no_source_items.append(item)
            families = {rule.family for rule in graph.rules_for(node) if rule.family}
            if families and families <= SYNTHETIC_FAMILIES:
                result.craft_only_items.append(item)
    return result


def base_entity(subject: str) -> str:
    """`minecraft:sheep/black` → `minecraft:sheep`。实体密度按基础类型测量。"""
    if not subject.startswith("minecraft:"):
        return subject
    name = subject.split(":", 1)[1]
    return f"minecraft:{name.split('/', 1)[0]}"


def read(path: Path) -> tuple[dict, list[SourceRecord]]:
    """读回来源索引。第一行是头部，其余每行一条来源。"""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"来源索引不存在：{path}；先跑 `bg index sources`") from exc

    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError(f"{path} 是空文件")
    header = json.loads(lines[0])
    if header.get("index_schema") != INDEX_SCHEMA:
        raise ValueError(
            f"{path} 的 index_schema={header.get('index_schema')!r}，本程序只认 {INDEX_SCHEMA}"
        )
    return header, [SourceRecord.from_dict(json.loads(line)) for line in lines[1:]]
