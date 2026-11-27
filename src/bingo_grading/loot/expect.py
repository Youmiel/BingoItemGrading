"""战利品表的期望产出。

## 算的是什么

「开一次箱子，平均得到多少件物品 X」。不是「能不能得到」——那是上游的事，
上游按存在性语义求解，刻意不算数量。

## 口径

```
E[X] = Σ_pool  E[rolls] × P(池条件)
              × Σ_entry∈pool  P(被选中) × E[数量]
```

- **P(被选中)** 按权重：`weight / Σweight`（缺省 weight 为 1，数据包里 645 条显式给了）。
- **E[数量]** 由 `set_count` 决定：`uniform` 取均值、`binomial` 取 `n·p`、整数直接取。
- **P(池条件)** 与 **P(条目条件)** 按条件类型分别处理，见 `_condition_probability`。
- `alternatives` 是「第一个满足条件的」：`P(第 i 个) = P(cond_i) × Π_{j<i}(1 − P(cond_j))`。
- `loot_table` 嵌套表递归求值，带环检测。

## 两处必须明说的近似

1. **工具条件是参数，不是常数**。`tool/can_silk_touch`、`tool/can_shear` 的
   成立与否取决于玩家带没带工具，而它直接改变结果：以橡树叶为例，带剪刀时
   掉树叶、木棍概率归零；徒手时掉树苗与木棍。所以 `expect()` 接受
   `tool_probability`，默认 1.0（带工具）。采集侧调用时两种都算，写成不同
   `condition` 的两行（见 `build.py`）。
2. **不计算时运/抢夺加成**。`table_bonus` 取 `chances[0]`（无附魔级），
   `enchanted_count_increase` / `apply_bonus` 不改变数量。也计入 `assumptions`。

两处都不静默：调用方拿到 `assumptions` 列表，可以据此判断结果是否适用于自己的场景。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from mcshared.loot import Entry, Table, local_id, normalize_table

from .tables import LootTableSet

# 无附魔时的加成等级索引。
NO_ENCHANT_LEVEL = 0

# 这些条件与「箱子给什么」无关：它们判的是爆炸、击杀者、伤害来源。
# 对箱子表来说恒成立；对实体/方块表则不该用本模块的期望（见 docstring）。
# 一律按 1.0 并记入 assumptions。
NEUTRAL_CONDITIONS = frozenset(
    {
        "minecraft:survives_explosion",
        "minecraft:killed_by_player",
        "minecraft:entity_properties",
        "minecraft:damage_source_properties",
        "minecraft:match_tool",
        "minecraft:location_check",
    }
)

ASSESSED_CONDITIONS = frozenset(
    {
        "minecraft:random_chance",
        "minecraft:random_chance_with_enchanted_bonus",
        "minecraft:table_bonus",
        "minecraft:any_of",
        "minecraft:all_of",
        "minecraft:inverted",
        "minecraft:tool/can_silk_touch",
        "minecraft:tool/can_shear",
        "minecraft:match_block",
    }
)

# `match_block` 判的是**被破坏的方块处在什么状态**。本项目只在一个场景下求方块表：
# 「玩家去采这块方块」。所以每个状态键都要假设一个取值，与假设不符的条目记 0。
#
# 这套取值就是 `docs/contracts/task-split.md` A8 的策略表：
# - `half=lower`：高植株取下半（挖高草是从下半动手，掉落按下半的表算），
#   上下半各有一条 pool，取 lower 才不会两边都算一次；
# - `part=head`：门/床这类 head/foot 结构同理；
# - `type=single`：台阶**不是双层**。斜石板表是「默认 1 个，双层时
#   `set_count 2`」，双层按成立会把一块方块算成 2 个。
MATCH_BLOCK_ASSUMED_STATE: dict[str, str] = {
    "half": "lower",
    "part": "head",
    "type": "single",
}

# `age` 按「成熟（最大 age）」处理：表里写的就是具体那个最大值（`age=7`），
# 而目标物品本来就是「收成熟的作物」。不按最大值判，作物表会整条归零。
MATCH_BLOCK_AGE_KEY = "age"

MAX_DEPTH = 8


@dataclass
class TableExpectation:
    """一张表的期望产出。"""

    table: str
    per_item: dict[str, float] = field(default_factory=dict)
    # 每件物品在一次开箱里的方差（与 `per_item` 同键）；供后续方差链路用。
    # **不进 `as_dict`**：那个结构是落盘格式，加字段会改变 `facts.jsonl`。
    variance: dict[str, float] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    # 期望进入箱子的物品总数（含未点名的）。
    total_items: float = 0.0
    # 求值时的工具条概率。1.0 = 带工具，0.0 = 徒手。
    tool_probability: float = 1.0

    def as_dict(self, digits: int = 4) -> dict:
        return {
            "table": self.table,
            "total_items": round(self.total_items, digits),
            "per_item": {
                item: round(value, digits)
                for item, value in sorted(self.per_item.items())
                if value
            },
            "assumptions": sorted(set(self.assumptions)),
            "unsupported": sorted(set(self.unsupported)),
        }


class _Ctx:
    """递归求值的上下文，收集近似与不支持项。"""

    def __init__(self, tool_probability: float = 1.0, tags: object | None = None) -> None:
        self.assumptions: set[str] = set()
        self.unsupported: set[str] = set()
        self.tool_probability = tool_probability
        # `tags` 是 `bingo_grading.tags.TagSet`；`None` 表示没有标签数据，`tag` 条目仍按未展开处理。
        self.tags = tags
        if tool_probability >= 1.0:
            self.assumptions.add("工具条件按「玩家持有该工具」计为 1.0")
        elif tool_probability <= 0.0:
            self.assumptions.add("工具条件按「徒手」计为 0.0")

    def assume(self, text: str) -> None:
        self.assumptions.add(text)

    def skip(self, text: str) -> None:
        self.unsupported.add(text)


def _number(value: object) -> float | None:
    """从 `{min,max}` / `{value}` / 标量里取期望值。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, dict):
        return None
    kind = str(value.get("type") or "")
    if kind.endswith("uniform"):
        low = value.get("min_inclusive", value.get("min"))
        high = value.get("max_inclusive", value.get("max"))
        if isinstance(low, (int, float)) and isinstance(high, (int, float)):
            low, high = float(low), float(high)
            if low < 0.0:
                # `set_count` 取到负数时游戏会 clamp 到 0（`LootItemFunctions`
                # 对结果取 `max(0, n)`），所以期望不是 `(min+max)/2`。
                # 6 处表用了负数下界：`wither_skeleton` / `spider` / `cave_spider`
                # 的 `uniform{-1,1}`（煤炭、蜘蛛眼），`magma_cube` 的 `{-2,1}`，
                # 两个蘑菇块的 `{-6,2}`。按 `(min+max)/2` 算会把 `{-1,1}` 算成
                # 0，下游据此认为「测得掉零个」，整条来源被判缺失。
                #
                # 离散均匀（MC 的 uniform 生成整数）：n 个取值各 1/n，
                # clamp 后只剩 1..high 贡献，和 `high·(high+1)/2`。
                if high <= 0.0:
                    return 0.0
                picks = high - low + 1.0
                return (high * (high + 1.0) / 2.0) / picks
            return (low + high) / 2
        return None
    if kind.endswith("binomial"):
        n = value.get("n")
        p = value.get("p")
        if isinstance(n, (int, float)) and isinstance(p, (int, float)):
            return float(n) * float(p)
        return None
    if kind.endswith("constant"):
        raw = value.get("value")
        return float(raw) if isinstance(raw, (int, float)) else None
    return None


def _cond_type(node: dict) -> str:
    """条件类型，出口统一补回 `minecraft:` 前缀。

    归一后的条件类型是局部名（`random_chance`），`inverted` / `any_of` 的
    `terms` 里还是全名。两种都收，后面的判定才只需要一套字面量。
    """
    raw = str(node.get("condition") or node.get("type") or "")
    if not raw:
        return ""
    return f"minecraft:{local_id(raw)}"


def _fn_type(node: dict) -> str:
    """函数类型，出口同 `_cond_type`：统一带 `minecraft:` 前缀。"""
    raw = str(node.get("type") or node.get("function") or "")
    if not raw:
        return ""
    return f"minecraft:{local_id(raw)}"


def _chance_at_level(chances: object, level: int) -> float | None:
    """附魔加成条件的成立概率。

    两种写法都要认：`chances` 是**数组**（按等级逐项，`table_bonus` 常见）或
    **分布对象**（`{"type": "linear", "start": 0.025, "steps": 0.01}`）。
    后者以前被整条丢掉，导致僵尸的铁锭掉率算成 0。
    """
    if isinstance(chances, list) and chances:
        value = chances[min(level, len(chances) - 1)]
        return float(value) if isinstance(value, (int, float)) else None
    if isinstance(chances, dict):
        kind = str(chances.get("type") or "")
        if kind.endswith("linear"):
            start = chances.get("start")
            steps = chances.get("steps", 0.0)
            if isinstance(start, (int, float)) and isinstance(steps, (int, float)):
                return float(start) + float(steps) * level
    return None


def _match_block_probability(node: dict, ctx: _Ctx) -> float:
    """`match_block` 条件成立的概率：按「采的是这块方块本身」逐键判状态。"""
    state = node.get("state")
    if not isinstance(state, dict) or not state:
        # 只限定了方块种类（`blocks`），没限定状态：采的就是它，成立。
        return 1.0
    for key, value in state.items():
        assumed = MATCH_BLOCK_ASSUMED_STATE.get(str(key))
        if assumed is None:
            if str(key) == MATCH_BLOCK_AGE_KEY:
                ctx.assume(f"policy:{MATCH_BLOCK_AGE_KEY} 按成熟（最大 age）计 1.0")
            else:
                ctx.assume(f"policy:{key} 未列入策略表，按成立计")
            continue
        if str(value) != assumed:
            return 0.0
        # 假设成立的条目也记一条：这一行产出是靠「假设方块是 X 状态」得来的。
        ctx.assume(f"policy:{key}={assumed}")
    return 1.0



def _condition_probability(node: dict, ctx: _Ctx, depth: int = 0) -> float:
    """条件成立的概率。无法判断时返回 0 并记 unsupported（宁可少算，不多算）。"""
    if depth > MAX_DEPTH:
        ctx.skip("条件嵌套过深")
        return 0.0

    kind = _cond_type(node)
    if not kind:
        ctx.skip(f"条件无类型：{sorted(node)}")
        return 0.0

    # Legacy names with the same meaning.
    if kind == "minecraft:alternative":
        kind = "minecraft:any_of"
    elif kind == "minecraft:block_state_property":
        return _match_block_probability({"state": node.get("properties")}, ctx)
    elif kind == "minecraft:random_chance_with_looting":
        chance = node.get("chance")
        ctx.assume("looting bonus assumed at enchantment level 0")
        return float(chance) if isinstance(chance, (int, float)) else 0.0

    if kind in ("minecraft:tool/can_silk_touch", "minecraft:tool/can_shear"):
        return ctx.tool_probability

    if kind == "minecraft:match_block":
        # 判的是**被破坏的方块处在什么状态**：作物成熟度（`age=7`）、
        # 高植株的上下半（`half`）、台阶是否双层（`type`）。
        #
        # 按 0 处理会让整张方块表期望为空（胡萝卜、马铃薯、小麦、瓶子草全走这条），
        # 下游只能退回「挖一块出 1 个」；而一律按 1 又会让「双层台阶掉两份」、
        # 「高草上下半各算一次」这类重复计数成立。所以按 `MATCH_BLOCK_ASSUMED_STATE`
        # 逐键判，只让与假设一致的那条成立。
        return _match_block_probability(node, ctx)

    if kind in NEUTRAL_CONDITIONS:
        # 与「箱子给什么」无关，按恒成立处理。
        return 1.0

    if kind == "minecraft:random_chance":
        chance = node.get("chance")
        return float(chance) if isinstance(chance, (int, float)) else 0.0

    if kind in ("minecraft:table_bonus", "minecraft:random_chance_with_enchanted_bonus"):
        # 26.x 写法是 `unenchanted_chance` + `enchanted_chance`（后者是分布对象），
        # 旧写法是 `chances` 数组。三种都收，取无附魔级。
        direct = node.get("unenchanted_chance")
        chance = float(direct) if isinstance(direct, (int, float)) else _chance_at_level(
            node.get("chances"), NO_ENCHANT_LEVEL
        )
        if chance is not None:
            ctx.assume("附魔加成按无附魔级（等级 0）计")
            return chance
        ctx.skip(f"条件 {kind} 的掉率无法解析：{sorted(node)}")
        return 0.0

    if kind == "minecraft:inverted":
        inner = node.get("term")
        if isinstance(inner, dict):
            # **中性条件的否定仍是中性**。`inverted(location_check{biomes:X})`
            # 说的是「这只箱子不在群系 X 里面」，与「箱子里装什么」无关；
            # 按 `1 - 1 = 0` 处理会把整池抹掉——营地补给箱的地图池就是
            # 8 个 `abandoned_camp_map` 条目各自排除一个群系，一乘全成 0，
            # 于是 `abandoned_camp_map` 永远没有期望。
            if _cond_type(inner) in NEUTRAL_CONDITIONS:
                ctx.assume("群系排他条件（inverted location_check）按中性计")
                return 1.0
            return 1.0 - _condition_probability(inner, ctx, depth + 1)
        ctx.skip("inverted 缺 term")
        return 0.0

    if kind == "minecraft:any_of":
        terms = [t for t in node.get("terms") or [] if isinstance(t, dict) or isinstance(t, str)]
        if not terms:
            return 0.0
        fail = 1.0
        for term in terms:
            prob = _condition_probability(_as_node(term), ctx, depth + 1)
            fail *= 1.0 - prob
        return 1.0 - fail

    if kind == "minecraft:all_of":
        terms = [t for t in node.get("terms") or [] if isinstance(t, dict) or isinstance(t, str)]
        if not terms:
            return 0.0
        product = 1.0
        for term in terms:
            product *= _condition_probability(_as_node(term), ctx, depth + 1)
        return product

    ctx.skip(f"条件类型未处理：{kind}")
    return 0.0


def _as_node(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return {"condition": value}
    return {}


def _conditions_probability(conditions: tuple[dict, ...], ctx: _Ctx) -> float:
    """一组条件同时成立的概率（AND）。条件在加载时已经归一，这里只管概率。"""
    if not conditions:
        return 1.0
    product = 1.0
    for condition in conditions:
        product *= _condition_probability(condition, ctx)
    return product


def _modifier_condition_probability(modifier: dict, ctx: _Ctx) -> float:
    """modifier 自己的条件成立概率。

    26.x 写的是单数 `condition`（对象）；老格式/手写可能出现复数 `conditions`。
    两者都收。以前整条不算，于是「双层台阶 `set_count 2`」这类**带条件的数量**
    会被无条件应用：`minecraft:blocks/oak_slab` 算成 2 个而不是 1 个。
    """
    probability = 1.0
    single = modifier.get("condition")
    if isinstance(single, dict):
        probability *= _condition_probability(single, ctx)
    many = modifier.get("conditions")
    if isinstance(many, (list, tuple)):
        probability *= _conditions_probability(
            tuple(item for item in many if isinstance(item, dict)), ctx
        )
    return probability



def _expected_count(functions: tuple[dict, ...], ctx: _Ctx) -> float:
    """一套 modifier 之后的数量期望。只看会改变数量的，其余忽略。"""
    count = 1.0
    for modifier in functions:
        kind = _fn_type(modifier)
        if kind == "minecraft:set_count":
            value = _number(modifier.get("count"))
            if value is None:
                ctx.skip(f"set_count 的 count 无法解析：{modifier.get('count')!r}")
            else:
                # 条件不成立时这个 modifier 整个不生效，数量保持原值。
                applied = _modifier_condition_probability(modifier, ctx)
                count = count * (1.0 - applied) + value * applied
        elif kind == "minecraft:limit_count":
            limit = modifier.get("limit") or {}
            high = _number(limit)
            if kind and high is not None:
                count = min(count, high)
        elif kind in ("minecraft:explosion_decay", "minecraft:set_damage"):
            # 只改变条件成立概率或损伤值，不改变数量。
            continue
        elif kind in (
            "minecraft:enchanted_count_increase",
            "minecraft:looting_enchant",
            "minecraft:apply_bonus",
        ):
            ctx.assume("不计附魔对数量的加成")
        elif kind in (
            "minecraft:enchant_randomly",
            "minecraft:enchant_with_levels",
            "minecraft:set_potion",
            "minecraft:set_name",
            "minecraft:set_enchantments",
            "minecraft:copy_components",
            "minecraft:copy_name",
            "minecraft:copy_nbt",
            "minecraft:set_contents",
            "minecraft:copy_state",
            "minecraft:set_stew_effect",
            "minecraft:set_ominous_bottle_amplifier",
            "minecraft:exploration_map",
            "minecraft:filtered",
            "minecraft:furnace_smelt",
        ):
            # 改变物品的附加数据，不改变数量与身份。
            continue
        else:
            ctx.skip(f"modifier 未处理：{kind}")
    return count


def _spread(value: object) -> float:
    """`count` 分布的方差。只认 uniform / binomial，其余按 0（常数）。

    注意这里用**原始**区间算方差：`_number` 会把负数下界 clamp 到 0，
    但 clamp 后的真实方差需要截断分布的推导，收益不值这个复杂度。
    负数区间只出现在 6 张表上（见 `uniform{min<0}` 的记忆），
    这些表的方差会略微高估——记在 assumptions 里。
    """
    if not isinstance(value, dict):
        return 0.0
    kind = str(value.get("type") or "")
    if kind.endswith("uniform"):
        low = value.get("min_inclusive", value.get("min"))
        high = value.get("max_inclusive", value.get("max"))
        if isinstance(low, (int, float)) and isinstance(high, (int, float)):
            span = float(high) - float(low) + 1.0  # 离散均匀的取值个数
            return max(0.0, (span * span - 1.0) / 12.0)
        return 0.0
    if kind.endswith("binomial"):
        n, p = value.get("n"), value.get("p")
        if isinstance(n, (int, float)) and isinstance(p, (int, float)):
            return float(n) * float(p) * (1.0 - float(p))
        return 0.0
    return 0.0


def _count_variance(functions: tuple[dict, ...], ctx: _Ctx) -> float:
    """一套 modifier 之后的数量方差，与 `_expected_count` 平行。

    多个 `set_count` 串联按「后一个覆盖前一个」处理（与均值口径一致）。
    """
    mean = 1.0
    variance = 0.0
    for modifier in functions:
        kind = _fn_type(modifier)
        if kind != "minecraft:set_count":
            continue
        value = modifier.get("count")
        new_mean = _number(value)
        if new_mean is None:
            continue
        new_variance = _spread(value)
        applied = _modifier_condition_probability(modifier, ctx)
        # 条件成立用新分布、不成立保持原值 → 混合分布的二阶矩
        mean_second = variance + mean * mean
        new_second = new_variance + new_mean * new_mean
        mixed_first = mean * (1.0 - applied) + new_mean * applied
        mixed_second = mean_second * (1.0 - applied) + new_second * applied
        mean = mixed_first
        variance = max(0.0, mixed_second - mixed_first * mixed_first)
    return variance


def _tag_members(entry: Entry, ctx: _Ctx) -> tuple[str, ...]:
    """`tag` 条目展开成物品列表。战利品表的标签在 `minecraft:item` 注册表下。"""
    tag = entry.tag_ref
    if ctx.tags is None or not tag:
        return ()
    expand = getattr(ctx.tags, "expand", None)
    if expand is None:
        return ()
    return tuple(expand(tag, registry="minecraft:item"))


def _add(target: dict[str, float], item: str, amount: float) -> None:
    if amount:
        target[item] = target.get(item, 0.0) + amount


def _only_subject_attributes(children: tuple[Entry, ...]) -> bool:
    """这些互斥分支是不是**只**靠主体属性区分（变种、方块状态、击杀者）。"""
    if len(children) < 2:
        return False
    for child in children:
        for condition in child.conditions:
            kind = _cond_type(condition)
            if kind not in NEUTRAL_CONDITIONS and kind not in (
                "minecraft:match_block",
                "minecraft:block_state_property",
            ):
                # 有随机率/工具条件混进来，就必须走「第一个成立者」那套。
                return False
    return True


def _entry_contribution(
    entry: Entry,
    share: float,
    ctx: _Ctx,
    tables: LootTableSet,
    depth: int,
    apply_conditions: bool = True,
    var: dict[str, float] | None = None,
    select_prob: float = 1.0,
) -> tuple[dict[str, float], float]:
    """一个条目在「被选中概率为 share」时的贡献：`(每物品期望, 总件数)`。

    `apply_conditions=False` 只用于 `alternatives` 的子条目：那个分支的概率
    已经由父层算过，再乘一遍就变成平方了。

    `var` 非空时同时累计方差：`share` 是期望次数，`select_prob` 是单次 roll
    选中该条目的概率，二项项 `share·(1-p)·E[X]²` 由它决定。
    """
    out: dict[str, float] = {}
    total = 0.0

    if depth > MAX_DEPTH:
        ctx.skip("嵌套表过深")
        return out, total

    if apply_conditions:
        # 条目自带的条件（如树叶的 2% 掉木棍）决定它会不会被给。
        # 不乘这一项，概率会变成 1，整个期望虚高。
        share *= _conditions_probability(entry.conditions, ctx)
        if share <= 0:
            return out, total

    if entry.kind == "empty":
        return out, total

    if entry.kind == "item" and entry.name:
        mean_count = _expected_count(entry.functions, ctx)
        amount = share * mean_count
        _add(out, entry.name, amount)
        total += amount
        if var is not None:
            # N ~ Binomial(rolls, p)：Var = share·Var(X) + share·(1-p)·E[X]²
            var_add = share * _count_variance(entry.functions, ctx)
            var_add += share * (1.0 - select_prob) * mean_count * mean_count
            _add(var, entry.name, max(0.0, var_add))
        return out, total

    if entry.kind == "loot_table":
        name = entry.nested_table
        if name:
            nested = tables.get(name)
            if nested is None:
                ctx.skip(f"嵌套表不存在：{name}")
                return out, total
            child_var: dict[str, float] = {}
            nested_out, nested_total = _table_contribution(
                nested, ctx, tables, depth + 1, var=child_var if var is not None else None
            )
            for item, value in nested_out.items():
                _add(out, item, value * share)
            if var is not None:
                for item, value in child_var.items():
                    _add(var, item, value * share)
            return out, nested_total * share
        inline = entry.inline_table
        if inline is not None:
            # `equipment/*` 把表直接写在 `value` 里，没有表 ID。
            nested = normalize_table(inline, "<inline>")
            child_var: dict[str, float] = {}
            nested_out, nested_total = _table_contribution(
                nested, ctx, tables, depth + 1, var=child_var if var is not None else None
            )
            for item, value in nested_out.items():
                _add(out, item, value * share)
            if var is not None:
                for item, value in child_var.items():
                    _add(var, item, value * share)
            return out, nested_total * share
        ctx.skip("嵌套表条目缺表 ID")
        return out, total

    if entry.kind in ("group", "sequence"):
        # 全部子条目都给，按权重分摊没有意义，各自按 share 计。
        for child in entry.children:
            child_out, child_total = _entry_contribution(
                child, share, ctx, tables, depth + 1, var=var, select_prob=select_prob
            )
            for item, value in child_out.items():
                _add(out, item, value)
            total += child_total
        return out, total

    if entry.kind == "alternatives":
        # 互斥分支**只靠主体属性区分**时（鸡的变种决定蛋的颜色、方块状态决定
        # 掉什么），这些条件对「我去猎取的目标」恒成立——想拿蓝蛋就去寒带养鸡。
        # 游戏数学上它们互斥，但模型里每条分支都各自可达，所以各计满 share。
        #
        # 不这么处理的话，`NEUTRAL_CONDITIONS` 把这些条件算成 1.0，第一个分支
        # 会把 `remaining` 吃光：`gameplay/chicken_lay` 只剩普通鸡蛋，蓝/棕蛋
        # 的期望恒为 0，整条来源被判「表里没有这个物品」。
        if _only_subject_attributes(entry.children):
            ctx.assume("互斥分支只按主体属性区分，按「目标就是那一种」各计 1.0")
            for child in entry.children:
                child_out, child_total = _entry_contribution(
                    child, share, ctx, tables, depth + 1, apply_conditions=False,
                    var=var, select_prob=1.0,
                )
                for item, value in child_out.items():
                    _add(out, item, value)
                total += child_total
            return out, total
        # 逐个尝试，第一个条件成立者生效。子条目的条件在这里算，
        # 所以向下传 apply_conditions=False。
        remaining = share
        for child in entry.children:
            probability = _conditions_probability(child.conditions, ctx)
            effective = remaining * probability
            if effective:
                child_out, child_total = _entry_contribution(
                    child, effective, ctx, tables, depth + 1, apply_conditions=False,
                    var=var, select_prob=probability,
                )
                for item, value in child_out.items():
                    _add(out, item, value)
                total += child_total
            remaining *= 1.0 - probability
            if remaining <= 0:
                break
        return out, total

    if entry.kind == "tag":
        # 标签条目的权重在成员间均分。没有标签数据时整条按未展开计（宁少不多）。
        members = _tag_members(entry, ctx)
        if not members:
            ctx.skip(f"tag 条目未展开：{entry.tag_ref}")
            return out, total
        mean_count = _expected_count(entry.functions, ctx)
        amount = share / len(members) * mean_count
        for member in members:
            _add(out, member, amount)
            total += amount
            if var is not None:
                # 成员间也是二项分布：单成员被选中的概率 ~ 1/len(members)
                var_add = (share / len(members)) * _count_variance(entry.functions, ctx)
                var_add += (share / len(members)) * (1.0 - 1.0 / len(members)) * mean_count * mean_count
                _add(var, member, max(0.0, var_add))
        return out, total

    if entry.kind == "dynamic":
        ctx.skip(f"dynamic 条目无法静态求值：{entry.name}")
        return out, total

    ctx.skip(f"条目类型未处理：minecraft:{entry.kind}")
    return out, total


def _table_contribution(
    table: Table,
    ctx: _Ctx,
    tables: LootTableSet,
    depth: int = 0,
    var: dict[str, float] | None = None,
) -> tuple[dict[str, float], float]:
    out: dict[str, float] = {}
    total = 0.0

    for pool in table.pools:
        rolls = _number(pool.rolls)
        if rolls is None:
            ctx.skip(f"rolls 无法解析：{pool.rolls!r}")
            continue
        pool_probability = _conditions_probability(pool.conditions, ctx)
        weights = pool.weight_total
        if not pool.entries or weights <= 0:
            continue
        for entry in pool.entries:
            weight_share = entry.weight / weights
            share = rolls * pool_probability * weight_share
            entry_out, entry_total = _entry_contribution(
                entry, share, ctx, tables, depth,
                var=var, select_prob=pool_probability * weight_share,
            )
            for item, value in entry_out.items():
                _add(out, item, value)
            total += entry_total

    return out, total


def expect(
    table_id: str,
    tables: LootTableSet,
    *,
    tool_probability: float = 1.0,
    tags: object | None = None,
) -> TableExpectation:
    """求一张表的期望产出。

    `tool_probability` 是「带工具」的成立概率：1.0 = 带剪刀/精准采集，
    0.0 = 徒手。两种都要算，因为它们给出不同的结果（见模块 docstring）。
    `tags` 是 `bingo_grading.tags.TagSet`，给了才能展开 `tag` 条目。
    """
    table = tables.require(table_id)
    ctx = _Ctx(tool_probability=tool_probability, tags=tags)
    variance: dict[str, float] = {}
    per_item, total = _table_contribution(table, ctx, tables, var=variance)
    # 负期望在物理上不可能。它来自 `set_count` 的负数下界被当成均值
    # （`brown_mushroom_block` 的表就会算出 -2 个蘑菇），如果放任它流下去，
    # 下游会生成「产出 -2 个」的动作并直接抛错。这里置零并记账，
    # 让问题露在 assumptions 里而不是炸在别处。
    negative = sorted(item for item, value in per_item.items() if value < 0)
    if negative:
        ctx.assume(f"负期望已置零：{', '.join(negative)}")
        per_item = {item: value for item, value in per_item.items() if value > 0}
    return TableExpectation(
        table=table_id,
        per_item=per_item,
        variance={item: value for item, value in variance.items() if item in per_item},
        assumptions=sorted(ctx.assumptions),
        unsupported=sorted(ctx.unsupported),
        total_items=total,
        tool_probability=tool_probability,
    )


def expect_all(
    tables: LootTableSet,
    prefix: str = "",
    *,
    tool_probability: float = 1.0,
    tags: object | None = None,
) -> list[TableExpectation]:
    """对全部（或以 `prefix` 开头的）表求期望。"""
    ids = [table_id for table_id in tables.ids() if table_id.startswith(prefix)]
    return [
        expect(table_id, tables, tool_probability=tool_probability, tags=tags)
        for table_id in ids
    ]


def probability_at_least_once(per_chest: float, chests: float) -> float:
    """开 `chests` 个箱子，至少拿到 1 件的概率。按泊松近似。"""
    if per_chest <= 0 or chests <= 0:
        return 0.0
    return 1.0 - math.exp(-per_chest * chests)
