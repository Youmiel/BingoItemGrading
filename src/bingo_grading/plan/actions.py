"""动作：一条来源的最小可比较形式。

一个**动作**是「做一次」：

| 字段 | 含义 |
|---|---|
| `cost` | 做一次的固定成本（TU）：旅行、开箱、走矿道之外的杂项 |
| `yields` | 做一次的产出向量（物品 → 期望件数） |
| `capacity` | 最多能做几次（`inf` 表示没有上限） |
| `requires` | 必须先到手的东西，带用量与模式 |

产出写成向量而不是单个数字，是因为「挖一块矿同时出石头」「开一个箱子同时出
五六种物品」这些产出不该各付一次旅行成本。成本挂在动作上，按「做几次」计。

`capacity` 记的是**次数**，不是件数。件数上限 = `capacity × yields[item]`。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

MODE_ONCE = "once"
MODE_PER_USE = "per_use"
MODES = (MODE_ONCE, MODE_PER_USE)

# 假物品的前缀。工具、站点、维度入口不是真物品，但它们的成本结构与物品一致，
# 所以照样建模成「某个动作的产出」。报告里靠这个前缀区分。
CAPABILITY_PREFIX = "capability:"

# `action_id` 的合法前缀（`"loot:villages"` 里的 `loot`）。
#
# 前缀不是装饰，三处逻辑按它分支：
#
# | 用它的地方 | 判什么 |
# |---|---|
# | `ACTION_KINDS[].discrete` | 一次调用能不能按件拆（开箱不能拆半次） |
# | `Requirement.forbid` | 来源黑名单按前缀匹配 |
# | `sources/actions/build.py` | 按前缀分类记账 |
#
# 拼错一个字母不会报错，只会让上面某个判断静默失效——所以这里列全，
# 并用 `tests/test_action_prefixes.py` 钉住「代码里用到的都在表里」。


@dataclass(frozen=True)
class ActionKind:
    """一个动作前缀的元数据：计数口径 + 理由。

    `discrete` 决定「凑 N 件」怎么计次（见 `plan/whole.py`）：

    - `True`：一次调用是一个**不可分割的物理事件**（跑一趟、杀一只、交互一次），
      计 `ceil(N / 单次产出)`。事件可能一次给多件（开箱 900 个煤），也可能概率性地
      给不到一件（游荡 0.08 件/次），但玩家没法只做 60%。
    - `False`：可以**按件线性重复**，计 `N / 单次产出`。合成 4 个木板只需 1/4 次
      合成的时间，多余产物可留用；挖一块矿就是一块矿的时间。

    单次产出恰好为 1 时两种口径同值，仍要分清语义——否则以后某个来源的产出
    变成多件时会悄悄算错（`kill:` 就是这么漏掉过一次）。
    """

    prefix: str
    discrete: bool
    why: str


# 全部前缀的计数口径。顺序沿用原来的分组，方便与源码对照。
ACTION_KINDS: tuple[ActionKind, ...] = (
    # —— 采集与地点
    ActionKind(
        "mine",
        False,
        "挖方块按块线性：挖 N 块就是 N 块的时间。",
    ),
    ActionKind(
        "loot",
        True,
        "跑一趟结构开箱：一趟里开几个箱是固定的，不能只跑半趟。",
    ),
    ActionKind(
        "loot_block",
        True,
        "挖掉一整块结构方块才拿到掉落物（雪块 4 个雪球、一次最多 28 件），不能挖半块。",
    ),
    ActionKind(
        "brush",
        True,
        "刷一块可疑方块是一次动作，产出按次结算（陶片 1 件/块）。",
    ),
    ActionKind(
        "dig",
        True,
        "嗅探兽刨一次地有固定冷却（8 分钟），不能刨半次。",
    ),
    ActionKind(
        "harvest",
        True,
        "打一次 creaking heart 的守护者才掉树脂，事件不可分割。",
    ),
    ActionKind(
        "sniffer",
        True,
        "孵化嗅探兽蛋并养到成年是一次事件。",
    ),
    ActionKind(
        "plant",
        True,
        "种一株作物是一次动作。",
    ),
    ActionKind(
        "grow",
        True,
        "等海龟长成一次。",
    ),
    ActionKind(
        "bloom",
        True,
        "放一晚等花自己睁开一次。",
    ),
    ActionKind(
        "lay",
        True,
        "鸡下一次蛋有冷却（5–10 分钟），不能下半个。",
    ),
    ActionKind(
        "bucket",
        True,
        "舀一桶液体是一次动作（`bucket:lava@lake` 一趟舀 10 桶）。",
    ),
    ActionKind(
        "fill",
        True,
        "给桶装一次内容物（细雪桶）。",
    ),
    ActionKind(
        "scoop",
        True,
        "用桶捞一只生物是一次动作。",
    ),
    ActionKind(
        "scoop_spawn",
        True,
        "等一只生物刷出来再捞，不能算半只。",
    ),
    ActionKind(
        "interact",
        True,
        "取一次蜂蜜这类交互是一次动作。",
    ),
    ActionKind(
        "shear",
        True,
        "剪一只羊一次给 2 个毛，不能剪半只。",
    ),
    ActionKind(
        "water",
        True,
        "走到水边装一次水。",
    ),
    ActionKind(
        "roam",
        True,
        "在群系里游荡等一次刷怪：产出是概率期望（常见 0.08 件/次），不能只游荡 8%。",
    ),
    ActionKind(
        "travel",
        True,
        "跑一趟路程（到达某个结构）是一次事件。",
    ),
    ActionKind(
        "portal",
        True,
        "造一次传送门（凑齐 10 块黑曜石并点火），不能造半个。",
    ),
    ActionKind(
        "end",
        True,
        "一次末地事件（搭桥横渡、进末地）。",
    ),
    ActionKind(
        "capability",
        True,
        "获得一项能力/状态一次。",
    ),
    ActionKind(
        "cast",
        True,
        "一次浇铸产出 10 块黑曜石，不能浇 1 块。",
    ),
    ActionKind(
        "igniter",
        True,
        "点一次火（点燃传送门/营火），不能点半个。",
    ),
    # —— 生物
    ActionKind(
        "kill",
        True,
        "杀一只：铁傀儡掉 3–5 个铁锭，没有「杀 0.75 只」。",
    ),
    ActionKind(
        "spawner",
        True,
        "刷怪笼等一只怪刷出来再杀，一次一只。",
    ),
    ActionKind(
        "haunt",
        True,
        "一次闹鬼事件（等待 + 击杀），产出按次结算。",
    ),
    ActionKind(
        "raid",
        True,
        "打完整场袭击（多波），不能打半场。",
    ),
    ActionKind(
        "wither",
        True,
        "打一次凋灵，一次掉 5–11 朵玫瑰。",
    ),
    ActionKind(
        "dragon",
        True,
        "打一次末影龙（实测约 6 分钟），龙死后才生成龙蛋。",
    ),
    ActionKind(
        "special",
        True,
        "触发一次特殊事件（幻翼膜 0.5 件/次）。",
    ),
    # —— 制造与变换
    ActionKind(
        "craft",
        False,
        "合成可反复做、多余产物可留用：一次 4 个木板按每件 1/4 次摊。",
    ),
    ActionKind(
        "smelt",
        False,
        "烧一件=一件，线性。",
    ),
    ActionKind(
        "transmute",
        False,
        "转化一块=一块，线性。",
    ),
    ActionKind(
        "convert",
        False,
        "转换一件，线性。",
    ),
    ActionKind(
        "wear",
        True,
        "铁砧磨坏一级：一次调用是「一直修东西到掉级」这个完整事件，"
        "12% 概率已折进成本（期望 8.3 次 × 3.31s），产出确定 1 件。",
    ),
    ActionKind(
        "oxidize",
        False,
        "铜氧化一块=一块。注意这是**并行等待**（放 10 块同时等 27 分钟），"
        "不是串行重复；当前成本模型仍按单价乘件数，批量偏保守（高估）。",
    ),
    ActionKind(
        "tool",
        False,
        "工具成本是合成路线的标注（`tool:craft:<物品>`），跟着合成线性。",
    ),
    # —— 交易与社交
    ActionKind(
        "trade",
        True,
        "一次交易一单，可能一次给 18 件，不能只买 1/18 单。",
    ),
    ActionKind(
        "gift",
        True,
        "一次英雄赠礼。",
    ),
    ActionKind(
        "cat_gift",
        True,
        "猫每天早晨送一次。",
    ),
    ActionKind(
        "bonus",
        True,
        "出生奖励箱只有一次。",
    ),
    ActionKind(
        "barter",
        True,
        "扔一次金锭换一次。",
    ),
    ActionKind(
        "train",
        True,
        "把村民养一级是一次。",
    ),
    ActionKind(
        "fish",
        True,
        "抛一次竿等咬钩，产出按次结算（常见 0.09–1 件/次）。",
    ),
    # —— 模板绑定（陶片这类挂在模板 NBT 里的物品）
    ActionKind(
        "bind",
        True,
        "跑一趟结构挖容器（陶片罐/箱子/熔炉…）拿模板里放的物品："
        "一次响应一个容器，不能挖半个。",
    ),
)

# 表里的裸前缀（不含冒号）。`Requirement.forbid` 与分类记账按它匹配。
ACTION_PREFIXES: tuple[str, ...] = tuple(kind.prefix for kind in ACTION_KINDS)

# 运行时校验用。`tuple` 保留给测试与文档阅读，查找走集合。
PREFIX_SET = frozenset(ACTION_PREFIXES)

# 不可分割的行动单元（带冒号，供 `startswith` 使用）。
# 由 `ACTION_KINDS` 派生，单一事实来源；理由见各条 `why`。
# 新代码请用 `is_discrete()` / `kind_of()`：它们按冒号前的整段精确匹配，
# 比 `startswith` 更明确（`loot_block:` 不会被误当成 `loot:`）。
ATOMIC_PREFIXES: tuple[str, ...] = tuple(
    f"{kind.prefix}:" for kind in ACTION_KINDS if kind.discrete
)

# 前缀 → 类别，O(1) 精确查表。
KIND_BY_PREFIX: dict[str, ActionKind] = {
    kind.prefix: kind for kind in ACTION_KINDS
}


def kind_of(action_id: str) -> ActionKind | None:
    """`action_id` 的类别；裸名（测试夹具里的 `"make"`）或未登记前缀返回 `None`。"""
    head, sep, _ = action_id.partition(":")
    if not sep:
        return None
    return KIND_BY_PREFIX.get(head)


def is_discrete(action_id: str) -> bool:
    """一次调用是否不可分割（按整次计次）。

    未登记前缀按「可分割」处理：`Action.__post_init__` 已经拦下带前缀的未登记项，
    走到这里说明是裸名，只在测试夹具里出现。
    """
    kind = kind_of(action_id)
    return bool(kind and kind.discrete)



class ActionError(ValueError):
    """动作定义不合法。"""


def is_capability(item: str) -> bool:
    """这条「物品」是不是能力/站点这类假物品。"""
    return item.startswith(CAPABILITY_PREFIX)


@dataclass(frozen=True)
class Requirement:
    """一个前置：某件东西、要多少、按什么模式付。

    - `once`：本方案里只付一次（工具、站点、打火石）。与用几次无关。
    - `per_use`：每次执行动作都要再拿一份（黑曜石、火焰弹、燃料）。

    `forbid` 是**来源前缀黑名单**：这个前置不许用那几条来源满足。
    它不是「这条来源很差」而是「这个用法在游戏里做不到」——例：
    `portal:patch_ruined` 要的是「现成的黑曜石块」，而浇筑出来的是
    「自己造一块」：在废弃传送门已有的框架缺口上倒岩浆与水，
    操作难度和从零浇一整框完全不是一回事，不能让它来抬价。
    黑名单按 `action_id.startswith(prefix)` 匹配。

    `forbid_needs` 是**自举约束**：不许用「不先拿到这些物品就拿不到」的来源。
    例：开下界的门不能用「去下界结构里挖的黑曜石」——那是先有门才能拿到的。
    判断依赖「不用目标时能不能拿到」的不动点，不是看静态依赖图（后者会
    把「铁锭也能从下界猪灵换」当成「铁锭必须下界」）。
    """

    item: str
    qty: float = 1.0
    mode: str = MODE_ONCE
    forbid: tuple[str, ...] = ()
    forbid_needs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.item:
            raise ActionError("前置物品名不能为空")
        if not self.qty > 0:
            raise ActionError(f"前置 {self.item} 的用量必须为正：{self.qty}")
        if self.mode not in MODES:
            raise ActionError(
                f"前置 {self.item} 的模式 {self.mode!r} 不在 {MODES} 内"
            )
        for prefix in self.forbid:
            if not prefix:
                raise ActionError(f"前置 {self.item} 的来源黑名单里不能有空串")
        for name in self.forbid_needs:
            if not name:
                raise ActionError(f"前置 {self.item} 的自举约束里不能有空串")

    def as_dict(self) -> dict:
        out = {"item": self.item, "qty": self.qty, "mode": self.mode}
        if self.forbid:
            out["forbid"] = list(self.forbid)
        if self.forbid_needs:
            out["forbid_needs"] = list(self.forbid_needs)
        return out


@dataclass(frozen=True)
class Action:
    """一次可重复执行的动作。"""

    action_id: str
    item: str
    cost: float
    yields: dict[str, float] = field(default_factory=dict)
    capacity: float = math.inf
    requires: tuple[Requirement, ...] = ()
    label: str = ""
    # 数值出处：`loot_expectation:<subject>` / `structure_content:...` / `calibration`。
    source: str = ""
    # 同一组名 = 这些来源玩家**不知道会先碰到哪个**，跑图时碰到哪个算哪个。
    # 它们的速度相加（`1/μ = Σ1/μᵢ`），而不是「选最便宜的那条」。
    # 例：跑图时碰到村庄就翻箱、碰到废弃传送门就开——两件事不互斥。
    race: str | None = None
    # 可折算成主产物的其它产出：`{物品: 等效件数}`。
    # 例：一箱开出 0.38 锢 + 3.17 金粒，9 粒合 1 锢，那么开箱对「锢」的
    # 有效产出是 `0.38 + 3.17/9`。不这么做的话，引擎会把金粒当成**独立来源**
    # 各算一次全成本，联合产出就被重复计价了。
    equivalents: dict[str, float] = field(default_factory=dict)
    # 动作**自身**的一次性成本（带名字）：到达某层、挖一囗竖井这类。
    # 与 `requires` 里的 `once` 前置不同——那些是「先拿到某物」，这些是
    # 「这个动作本身要做一次的一次性开销」。需求 n 件时只收一次，
    # 否则「挖 N 个矿」会把同一口竖井重挖 N 遍。
    overhead: tuple[tuple[str, float], ...] = ()
    # 单次调用**有效主产出**的相对标准差（`sd / mean`）：只影响
    # `Segment.cv` → `Curve.variance()` / `tail_cost()`，**不参与 `unit_cost`**。
    # 0 = 当作确定量。目前能算出真值的只有 loot 表来源
    # （`expect.variance / expect.per_item²`），其余一律留 0（无信息，不惩罚）。
    yield_cv: float = 0.0

    def __post_init__(self) -> None:
        if not self.action_id:
            raise ActionError("动作 ID 不能为空")
        # 前缀白名单：拼错不会报错，只会让 `ATOMIC_PREFIXES` / `forbid` /
        # 分类记账静默失效。所以在构造时就拦下来（含动态拼接的那种）。
        #
        # 不带冒号的 ID 不校验：真实代码里每个 ID 都带前缀（见
        # `tests/test_action_prefixes.py` 的字面量扫描），裸名只出现在测试夹具里。
        head, sep, _ = self.action_id.partition(":")
        if sep and head not in PREFIX_SET:
            raise ActionError(
                f"动作 ID 的前缀未登记：{self.action_id!r}（前缀 {head!r}）；"
                f"合法前缀见 `plan/actions.py` 的 `ACTION_PREFIXES`"
            )
        if not self.item:
            raise ActionError(f"动作 {self.action_id} 的主产物不能为空")
        if not math.isfinite(self.cost) or self.cost < 0:
            raise ActionError(f"动作 {self.action_id} 的成本必须有限且非负：{self.cost}")
        if not self.capacity > 0:
            raise ActionError(f"动作 {self.action_id} 的容量必须为正：{self.capacity}")
        if not self.yields:
            raise ActionError(f"动作 {self.action_id} 没有任何产出")
        if self.race is not None and not self.race:
            raise ActionError(f"动作 {self.action_id} 的 race 组名不能为空串（不要并行就写 None）")
        for name, weight in self.equivalents.items():
            if name == self.item:
                raise ActionError(
                    f"动作 {self.action_id} 的 equivalents 里不能含主产物自己"
                )
            if not weight > 0:
                raise ActionError(
                    f"动作 {self.action_id} 的等价折算 {name} 必须为正：{weight}"
                )            # 不在产出里不算错：等价折算表常整表照传（金粒、金块都可能出现），
            # 具体到某个动作时某一项可能压根没产出，自然就是 0 权重。
        for name, amount in self.yields.items():
            if not amount > 0:
                raise ActionError(
                    f"动作 {self.action_id} 的产出 {name} 必须为正：{amount}"
                )
        if self.item not in self.yields:
            raise ActionError(
                f"动作 {self.action_id} 的产出里没有主产物 {self.item}"
            )
        for name, amount in self.overhead:
            if not name:
                raise ActionError(f"动作 {self.action_id} 的固定成本缺少名字")
            if amount < 0:
                raise ActionError(
                    f"动作 {self.action_id} 的固定成本不能为负：{name} = {amount}"
                )
        if not math.isfinite(self.yield_cv) or self.yield_cv < 0:
            raise ActionError(
                f"动作 {self.action_id} 的相对标准差必须有限且非负：{self.yield_cv}"
            )
        seen: set[str] = set()
        for req in self.requires:
            if req.item in seen:
                raise ActionError(
                    f"动作 {self.action_id} 对 {req.item} 写了多条前置，"
                    "合并成一条（模式不同要拆成两个动作）"
                )
            seen.add(req.item)
            if req.item in self.yields:
                raise ActionError(
                    f"动作 {self.action_id} 生产 {req.item} 又要求它，自相矛盾"
                )

    # ------------------------------------------------------------ 派生量

    @property
    def primary_yield(self) -> float:
        """做一次能得多少件主产物（不含等价折算）。"""
        return self.yields[self.item]

    def effective_yield(self, item: str) -> float:
        """做一次对 `item` 的**有效产出**：直出 + 可折算的其它产出。

        只有主产物会拿到折算。拿副产物当目标时仍然只是副产物本身的量。
        """
        direct = self.yields.get(item, 0.0)
        if item != self.item:
            return direct
        extra = sum(
            weight * self.yields.get(name, 0.0)
            for name, weight in self.equivalents.items()
        )
        return direct + extra

    @property
    def side_yields(self) -> dict[str, float]:
        """主产物之外的产出。判断「这个动作顺带给了什么」时用。"""
        return {name: amount for name, amount in self.yields.items() if name != self.item}

    def unit_cost(self, surcharge: float = 0.0) -> float:
        """每件**有效**主产物的成本。`surcharge` 是先摊进来的前置成本。"""
        return (self.cost + surcharge) / self.effective_yield(self.item)

    def item_capacity(self) -> float:
        """这个动作总共最多能给出多少件主产物（含等价折算）。"""
        return self.capacity * self.effective_yield(self.item)

    def overhead_total(self) -> float:
        """动作自身的一次性成本合计（不含 `requires` 里的一次性前置）。"""
        return sum(amount for _, amount in self.overhead)

    def requirement_items(self) -> frozenset[str]:
        return frozenset(req.item for req in self.requires)

    def as_dict(self, *, full: bool = False) -> dict:
        """`full=True` 保留完整浮点，供落盘复用（图必须逐位往返）。

        默认对数值做舍入，因为这份输出主要给人读；落盘时舍入会让
        `whole_cost_tu` 的最后一位漂移，两种查询模式就对不上了。
        `overhead` 写有序对而不是对象：同名固定成本可以出现多次
        （不同纵段的竖井各收一次），写成对象会静默吞掉后一条。
        """
        number = (lambda value: value) if full else (lambda value: round(value, 4))
        weight = (lambda value: value) if full else (lambda value: round(value, 6))
        out = {
            "action_id": self.action_id,
            "item": self.item,
            "cost": number(self.cost),
            "yields": {
                name: weight(value) for name, value in sorted(self.yields.items())
            },
            "capacity": None if math.isinf(self.capacity) else self.capacity,
            "requires": [req.as_dict() for req in self.requires],
        }
        if self.label:
            out["label"] = self.label
        if self.source:
            out["source"] = self.source
        if self.race is not None:
            out["race"] = self.race
        if self.equivalents:
            out["equivalents"] = {
                name: weight(value)
                for name, value in sorted(self.equivalents.items())
            }
        if self.overhead:
            out["overhead"] = [[name, weight(amount)] for name, amount in self.overhead]
        if self.yield_cv:
            out["yield_cv"] = weight(self.yield_cv)
        return out

    @classmethod
    def from_dict(cls, raw: dict) -> "Action":
        capacity = raw.get("capacity")
        return cls(
            action_id=raw["action_id"],
            item=raw["item"],
            cost=raw["cost"],
            yields=dict(raw.get("yields") or {}),
            capacity=math.inf if capacity is None else capacity,
            requires=tuple(
                Requirement(
                    item=entry["item"],
                    qty=entry.get("qty", 1.0),
                    mode=entry.get("mode", MODE_ONCE),
                    forbid=tuple(entry.get("forbid") or ()),
                    forbid_needs=tuple(entry.get("forbid_needs") or ()),
                )
                for entry in raw.get("requires") or ()
            ),
            label=raw.get("label", ""),
            source=raw.get("source", ""),
            race=raw.get("race"),
            equivalents=dict(raw.get("equivalents") or {}),
            overhead=tuple((name, amount) for name, amount in raw.get("overhead") or ()),
            # 新字段必须显式透传：漏了的话落盘再读回来方差就静默归零
            # （`forbid` 当年就是这么丢的）。
            yield_cv=raw.get("yield_cv", 0.0),
        )


@dataclass(frozen=True)
class ActionSet:
    """一组动作，按主产物索引。"""

    label: str
    actions: tuple[Action, ...]

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for action in self.actions:
            if action.action_id in seen:
                raise ActionError(f"动作 ID 重复：{action.action_id}")
            seen.add(action.action_id)

    def by_item(self) -> dict[str, list[Action]]:
        index: dict[str, list[Action]] = {}
        for action in self.actions:
            index.setdefault(action.item, []).append(action)
        return index

    def items(self) -> list[str]:
        return sorted({action.item for action in self.actions})

    def produced_items(self) -> list[str]:
        """所有出现过的产出名，含副产物。"""
        return sorted({name for action in self.actions for name in action.yields})

    def get(self, action_id: str) -> Action:
        for action in self.actions:
            if action.action_id == action_id:
                return action
        raise ActionError(f"没有动作 {action_id!r}")

    def without(self, *action_ids: str) -> "ActionSet":
        """去掉若干动作。测试「少一条来源会怎样」时用，不改原集合。"""
        drop = set(action_ids)
        missing = drop - {action.action_id for action in self.actions}
        if missing:
            raise ActionError(f"要去掉的动作不存在：{sorted(missing)}")
        return ActionSet(
            label=self.label,
            actions=tuple(a for a in self.actions if a.action_id not in drop),
        )

    def as_dict(self, *, full: bool = False) -> dict:
        return {
            "label": self.label,
            "actions": [action.as_dict(full=full) for action in self.actions],
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "ActionSet":
        return cls(
            label=raw.get("label", ""),
            actions=tuple(Action.from_dict(entry) for entry in raw.get("actions") or ()),
        )

    def summary(self) -> dict:
        return {
            "label": self.label,
            "actions": len(self.actions),
            "items": len(self.items()),
            "max_capacity": sum(
                1 for action in self.actions if not math.isfinite(action.capacity)
            ),
        }
