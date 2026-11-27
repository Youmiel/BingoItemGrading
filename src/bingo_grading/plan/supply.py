"""供给曲线：一件物品「越要越贵」的成本表。

曲线是**凸的分段线性函数**：

```
f(n) = 取得 n 个单位的最小成本
```

每一段来自一个动作，段内单价恒定，段按单价升序排列。段与段的交界就是
「再要多就得换来源」的交叉点——便宜的先上场，用满容量才轮到贵的。
这一条就是「平行路线互相竞争」的全部机制，不需要额外规则。

为什么不沿用 `bingo_grading.graph` 的调和并联：`1 / Σ(1/w_i)` 隐含「同时走所有路」，
会把一条很便宜的路稀释成「也不便宜」，与「竞争淘汰绕路」正好相反。
它适合的是**同域可并行**的场景，而那个场景在这里就是容量相加。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from itertools import combinations
from typing import Iterable, Mapping, Sequence

from .actions import Action

# 浮点比较的容差：容量与件数都是「可数个」，差这一点点不该判成超容。
EPS = 1e-9

# 求「哪几段凑够 n 件」时最多枚举多少段的子集。
#
# 有固定成本之后，最优解不再是一条连续后缀：可能出现「用第 1 段 + 第 3 段、
# 跳过中间那段」。段数很少（真实曲线 3–6 段），所以直接枚举子集求精确解；
# 超过这个数才退回「跳过后缀」的旧族，避免 2^n 爆掉。
MAX_ENUM_SEGMENTS = 12

# 尾部成本的分位系数：正态单侧 95%。
#
# `Curve.tail_cost()`（均摊曲线）与 `grade` 的整次口径 p95 都用它——两处必须是
# 同一个数，否则「尾部 TU」与「整次 TU」的差就不是纯粹的口径差了。
TAIL_Z = 1.645


def merge_fixed(*groups: Iterable[tuple[str, float]]) -> tuple[tuple[str, float], ...]:
    """把几组固定成本按**名字**合并：同一个前置只付一次。

    固定成本必须带名字（而不只是一个金额），否则跨层传递时无法判断
    「这两笔 2.422 是不是同一趟村庄」——那会导致重复计费。
    """
    # 绝大多数调用只传一组（单段候选）；直接透传，别搭字典。
    if len(groups) == 1:
        return tuple(groups[0])
    best: dict[str, float] = {}
    for group in groups:
        for name, amount in group:
            best[name] = max(best.get(name, 0.0), amount)
    return tuple(sorted(best.items()))


def fixed_total(fixed: Iterable[tuple[str, float]]) -> float:
    return sum(amount for _, amount in fixed)


class SupplyError(ValueError):
    """曲线构造不合法。"""


@dataclass(frozen=True)
class Segment:
    """曲线的一段：一个动作贡献的连续若干件，段内单价恒定。"""

    action_id: str
    unit_cost: float
    capacity: float
    start: float = 0.0
    label: str = ""
    # 这个来源在依赖图上的层号。同价时先用更早能拿到的那个，
    # 也用来在报告里说明「这段是第几层的来源」。
    level: int = 0
    # 并行来源合成后的成员：`((action_id, unit_cost), ...)`。
    # 空元组表示这一段就来自 `action_id` 本身。
    members: tuple[tuple[str, float], ...] = ()
    # 进入这一段要付一次的固定成本（`once` 前置：做一套工具、跑一趟地点、
    # 解锁一个状态）， **带名字**：`(("capability:village", 2.42), ...)`。
    # 带名字是为了跨层去重——同一条路线里的两笔同名固定成本就是同一件事。
    fixed: tuple[tuple[str, float], ...] = ()
    # 单次产出件数的**相对标准差**（`sd / mean`）。这一段的成本不是定数：
    # 凑 `n` 件的成本方差 ≈ `n · (unit_cost · cv)²`。0 表示当作确定性来源。
    # 目前只有 loot 面算得出真值（`loot/expect.py` 的 `variance / per_item²`），
    # 其余来源一律 0，报告里按「无方差信息」计，不惩罚。
    cv: float = 0.0

    def __post_init__(self) -> None:
        if self.unit_cost < 0:
            raise SupplyError(f"单价不能为负：{self.action_id} = {self.unit_cost}")
        if not self.capacity > 0:
            raise SupplyError(f"段容量必须为正：{self.action_id} = {self.capacity}")
        if self.start < 0:
            raise SupplyError(f"段起点不能为负：{self.action_id} = {self.start}")
        if not math.isfinite(self.cv) or self.cv < 0:
            raise SupplyError(
                f"相对标准差必须有限且非负：{self.action_id} = {self.cv}"
            )
        for name, amount in self.fixed:
            if amount < 0:
                raise SupplyError(
                    f"固定成本不能为负：{self.action_id} 的 {name} = {amount}"
                )

    @property
    def fixed_cost(self) -> float:
        """这一段自己的固定成本合计（不含其它段的同名项）。"""
        return fixed_total(self.fixed)

    @property
    def end(self) -> float:
        """这一段覆盖到第几件（累计，含）。"""
        return self.start + self.capacity

    def take(self, count: float) -> float:
        """在这段里取 `count` 件的**边际**花费（不超过段容量，不含固定成本）。"""
        return min(count, self.capacity) * self.unit_cost

    def as_dict(self) -> dict:
        out = {
            "action_id": self.action_id,
            "unit_cost": round(self.unit_cost, 6),
            "capacity": None if math.isinf(self.capacity) else round(self.capacity, 6),
            "start": round(self.start, 6),
            "end": None if math.isinf(self.end) else round(self.end, 6),
            "level": self.level,
        }
        if self.fixed_cost:
            out["fixed_cost"] = round(self.fixed_cost, 6)
        if self.fixed:
            out["fixed"] = {name: round(amount, 6) for name, amount in self.fixed}
        if self.members:
            out["members"] = [name for name, _ in self.members]
        if self.cv:
            out["cv"] = round(self.cv, 6)
        if self.label:
            out["label"] = self.label
        return out


@dataclass(frozen=True)
class Curve:
    """一组按单价升序排好、首尾相接的段。"""

    segments: tuple[Segment, ...] = ()

    # ------------------------------------------------------------ 构造

    @classmethod
    def empty(cls) -> "Curve":
        return cls()

    @classmethod
    def of(cls, segments: Iterable[Segment]) -> "Curve":
        """排序 + 丢掉空段 + 重算起点。

        按**拿 1 件的全价**（`固定 + 边际`）升序，不是按边际单价——有固定成本后
        两者会给出不同的顺序，而报告里的段顺序应当和「实际会先选哪条」一致。
        真正取哪几段由 `covering` 求最优决定（它会枚举起点），顺序只影响展示。
        """
        usable = [seg for seg in segments if seg.capacity > EPS]
        usable.sort(
            key=lambda seg: (seg.fixed_cost + seg.unit_cost, seg.level, seg.action_id)
        )
        ordered: list[Segment] = []
        start = 0.0
        for seg in usable:
            ordered.append(replace(seg, start=start))
            start += seg.capacity
        return cls(tuple(ordered))

    # ------------------------------------------------------------ 查询

    def __bool__(self) -> bool:
        return bool(self.segments)

    @property
    def total_capacity(self) -> float:
        return sum(seg.capacity for seg in self.segments)

    @property
    def head(self) -> Segment | None:
        """最便宜的段。"""
        return self.segments[0] if self.segments else None

    def excluding(self, *prefixes: str) -> "Curve":
        """去掉来源 id 以这些前缀开头的段。

        只在**这个前置不许用那几条来源**时才用（见 `Requirement.forbid`）：
        去掉之后要重新排序、重算段起点，所以走一遍 `Curve.of`。
        并行合成的段（`race`）看 `members`，任一个成员命中就算命中。
        """
        if not prefixes:
            return self

        def banned(seg: Segment) -> bool:
            ids = (seg.action_id, *(action_id for action_id, _ in seg.members))
            return any(name.startswith(p) for name in ids for p in prefixes)

        keep = tuple(seg for seg in self.segments if not banned(seg))
        if len(keep) == len(self.segments):
            return self
        return Curve.of(keep)

    def cost(self, count: float) -> float:
        """取得 `count` 件的**总价**：`去重后的固定成本 + 边际`。容量不够时为 `inf`。

        有固定成本之后，「按边际单价排序」不再等于「按总价最优」：
        一条边际 1.0 但固定 12.0 的来源，拿 1 件要 13，而边际 10.0、固定 0 的
        只要 10。所以这里**真的求最小**（枚举起点，段数很少）。
        """
        if count <= EPS:
            return 0.0
        return self._pick(count)[0]

    def marginal_cost(self, count: float) -> float:
        """取得 `count` 件时的**边际**部分（不含固定成本）。容量不够时为 `inf`。"""
        if count <= EPS:
            return 0.0
        return self._pick(count)[1]

    def fixed_cost(self, count: float) -> float:
        """取得 `count` 件里含的固定成本部分（`cost - marginal_cost`，已去重）。"""
        if count <= EPS:
            return 0.0
        return fixed_total(self._pick(count)[3])

    def variance(self, count: float) -> float:
        """取得 `count` 件的**成本方差**（只含段内随机性，固定成本当作确定）。

        单次调用的产出件数若有波动，凑够第 `i` 段的 `nᵢ` 件时成本方差近似
        `nᵢ · (unitᵢ · cvᵢ)²`——各次调用独立、`cv` 是单次产出的相对标准差。

        **段边界的跳变不在这里**：`cost()` 已经在最优组合上取了真值，跳变是
        确定性的。这里只补「同一组合内部运气好坏」的那部分，与 `cost` 并列，
        不乘进 `cost`。容量不够时返回 `inf`。
        """
        if count <= EPS:
            return 0.0
        used = self.covering(count)
        if not used:
            return math.inf
        remaining = count
        total = 0.0
        # `covering` 给的是**填充顺序**（按边际单价升序）；排序后重放一遍
        # 才能知道每段实际取了几件。
        for segment in sorted(used, key=lambda item: item.unit_cost):
            take = min(remaining, segment.capacity)
            if take <= EPS:
                break
            if segment.cv:
                total += take * (segment.unit_cost * segment.cv) ** 2
            remaining -= take
        return total

    def tail_cost(self, count: float, *, z: float = TAIL_Z) -> float:
        """取得 `count` 件的 **p95 成本**：`cost + z · sd`。容量不够时为 `inf`。

        `z` 默认 `TAIL_Z`（正态单侧 95%），与 `p95` 的采集口径一致，也是
        `grade` 整次口径用的同一个常数。这是**查询**，不改变 `cost()` 的口径
        ——要不要用它打分由调用方决定，`Plan.cost` 里仍是平均成本。
        """
        base = self.cost(count)
        if not math.isfinite(base):
            return base
        spread = self.variance(count)
        if not math.isfinite(spread):
            return math.inf
        return base + z * math.sqrt(spread)

    def _candidates(
        self, skip: int, order: Sequence[int], limit: int | None = None
    ) -> Iterable[int]:
        """候选取段组合，用**位掩码**表示（第 i 位 = 取 `segments[skip:][i]`）。

        返回掩码而不是元组：调用方要按**边际单价升序**填充，而 `order`
        已经把下标排好了。以前每个候选都 `sorted(group)` 一次，
        profile 里那一项占掉 `_pick` 的一半。

        `limit` 限制「最多取几段」。要 1 件时不可能需要三段以上
        （空段已经丢了，每段至少能供一件），所以只用枚举 1～2 段：
        候选数从 2^n 降到 n+C(n,2)，而 `count=1` 是绝大多数调用。

        段少时枚举子集；多时退回三个族：
        1. **按全价的前缀**——`segments` 已按「拿 1 件的全价」升序，
           就是「从最便宜的一件算起逐步纳入」。用前缀而不是后缀，是因为
           后缀会永远包含 unit 最低但固定极高的段（末地城 unit 0.54、固定 63.65）；
        2. **单段**——保证「只取 unit 最低的那一条」总在候选里；
        3. **按单价的前缀**——保证「跳过贵的固定成本、只要后面几条」也能选到。
        """
        n = len(self.segments) - skip
        if n <= 0:
            return
        if n <= MAX_ENUM_SEGMENTS:
            if limit is None or limit >= n:
                for mask in range(1, 1 << n):
                    yield mask
                return
            for size in range(1, limit + 1):
                for combo in combinations(range(n), size):
                    mask = 0
                    for index in combo:
                        mask |= 1 << index
                    yield mask
            return
        for end in range(1, n + 1):
            yield (1 << end) - 1
        for index in range(n):
            yield 1 << index
        ranked = 0
        for position in range(n):
            ranked |= 1 << order[position]
            if position >= 1:
                yield ranked

    def _pick(self, count: float, *, skip: int = 0) -> tuple[float, float, tuple[Segment, ...], tuple[tuple[str, float], ...]]:
        """该走哪几段：`(总价, 边际部分, 用到的段, 去重后的固定成本)`。

        固定成本按名字去重后再相加：同一条路线里两笔 `capability:village`
        就是同一趟村庄，不能收两次。容量不够时前三项是 `inf`。

        枚举**子集**而不是连续后缀：中间那段可能因为固定成本太贵而该整体跳过。
        给定子集后按**边际单价升序**填充——固定成本已经按名字合并、与填充顺序
        无关，所以这样填出来的边际最小。

        `order` 只算一次（按边际单价排好的下标），候选用位掩码枚举：
        这样每个候选的开销只有「遍历一遍 `order`」，不用再排序。
        """
        if count <= EPS:
            return 0.0, 0.0, (), ()
        segments = self.segments[skip:]
        n = len(segments)
        if n == 0:
            return math.inf, math.inf, (), ()
        order = sorted(range(n), key=lambda index: segments[index].unit_cost)
        # 需求量 ≤ 1 件时最多用两段（单段不够才会拆），没必要枚举 2^n。
        limit = 2 if count <= 1.0 + EPS else None
        best: tuple[float, float, tuple[Segment, ...], tuple[tuple[str, float], ...]] | None = None
        for mask in self._candidates(skip, order, limit):
            marginal = 0.0
            left = count
            used: list[Segment] = []
            for index in order:
                if not (mask >> index) & 1:
                    continue
                seg = segments[index]
                if seg.capacity >= left:
                    # 这一段就能包住剩下的量（容量无限时总走这里）。
                    marginal += seg.unit_cost * left
                    used.append(seg)
                    left = 0.0
                    break
                marginal += seg.unit_cost * seg.capacity
                used.append(seg)
                left -= seg.capacity
                if best is not None and marginal >= best[0]:
                    # 固定成本不为负，边际已经超了就不可能赢。
                    break
            if left > EPS:
                continue
            fixed = merge_fixed(*(seg.fixed for seg in used))
            total = fixed_total(fixed) + marginal
            if best is None or total < best[0] - EPS:
                best = (total, marginal, tuple(used), fixed)
        if best is None:
            return math.inf, math.inf, (), ()
        return best

    def unit_cost_at(self, position: float) -> float:
        """第 `position` 件（1 起算）的边际单价。超出容量时抛错。"""
        if position <= EPS:
            raise SupplyError(f"件数必须为正：{position}")
        for seg in self.segments:
            if position <= seg.end + EPS:
                return seg.unit_cost
        raise SupplyError(
            f"曲线只覆盖 {self.total_capacity:g} 件，问不到第 {position:g} 件"
        )

    def segment_at(self, position: float) -> Segment:
        """第 `position` 件落在哪一段（按**总价最优**那条路）。"""
        if position <= EPS:
            raise SupplyError(f"件数必须为正：{position}")
        total = 0.0
        for seg in self.covering(position):
            total += seg.capacity
            if position <= total + EPS:
                return seg
        raise SupplyError(
            f"曲线只覆盖 {self.total_capacity:g} 件，问不到第 {position:g} 件"
        )

    def covering(self, count: float, *, skip: int = 0) -> tuple[Segment, ...]:
        """要凑够 `count` 件会用到哪些段——按**总价**选，不是按边际单价。

        `skip` 表示「前几段不许用」（调用方想看看换一条路会不会更便宜）。
        """
        if count <= EPS:
            return ()
        return self._pick(count, skip=skip)[2]

    def breaks(self) -> tuple[float, ...]:
        """交叉点：累计件数超过这些值就该换来源了。"""
        return tuple(seg.end for seg in self.segments[:-1])

    def crossovers(self) -> tuple[tuple[Segment, Segment], ...]:
        """相邻段的（前一段，后一段），用来报告「多少件以上换路线」。"""
        return tuple(zip(self.segments, self.segments[1:]))

    # ------------------------------------------------------------ 变换

    def compose(self, *, ratio: float = 1.0, fixed: float = 0.0) -> "Curve":
        """串联变换：每产出 `ratio` 件成品要消耗 1 件输入，再加 `fixed` 固定成本。

        `f_child(n) = f_parent(n / ratio) + fixed × (n / ratio)`

        所以每件的单价变成 `(父单价 + fixed) / ratio`，容量翻 `ratio` 倍。
        这里用的是连续松弛：真实配方是「凑够 ratio 件才做一次」，
        件数很少时用 `ceil` 更准，报告里有 `exact` 口径另行核对。
        """
        if not ratio > 0:
            raise SupplyError(f"配方产量必须为正：{ratio}")
        if fixed < 0:
            raise SupplyError(f"固定成本不能为负：{fixed}")
        return Curve.of(
            Segment(
                action_id=seg.action_id,
                unit_cost=(seg.unit_cost + fixed) / ratio,
                capacity=seg.capacity * ratio,
                label=seg.label,
                level=seg.level,
                # 固定成本不随产量放大：做一次配方只付一次。
                fixed=seg.fixed,
                # 串联后「每件成品」的波动被产量摊薄：
                # `Var_out = Var_in / ratio²` 换算到新单价上就是下面这个因子。
                cv=(
                    seg.cv
                    * seg.unit_cost
                    / ((seg.unit_cost + fixed) * math.sqrt(ratio))
                    if seg.cv and seg.unit_cost + fixed > 0
                    else 0.0
                ),
            )
            for seg in self.segments
        )

    def limited(self, capacity: float) -> "Curve":
        """把曲线的总容量截到 `capacity` 件。"""
        if capacity <= EPS:
            return Curve.empty()
        out: list[Segment] = []
        left = capacity
        for seg in self.segments:
            take = min(left, seg.capacity)
            if take > EPS:
                out.append(replace(seg, capacity=take))
            left -= seg.capacity
            if left <= EPS:
                break
        return Curve.of(out)

    def as_dict(self) -> dict:
        return {
            "capacity": (
                None if math.isinf(self.total_capacity) else round(self.total_capacity, 6)
            ),
            "breaks": [
                None if math.isinf(value) else round(value, 6) for value in self.breaks()
            ],
            "segments": [seg.as_dict() for seg in self.segments],
        }


def merge(*curves: Curve) -> Curve:
    """OR：同一物品的多个来源合成一条曲线，便宜的先用。"""
    segments = [seg for curve in curves for seg in curve.segments]
    return Curve.of(segments)


def merge_race(segments: Sequence[Segment], *, label: str = "") -> Segment:
    """并行来源合成一段：速率相加 `1/μ = Σ1/μᵢ`。

    这是「跑图时碰到哪个算哪个」的语义，与「选最便宜的那条」不同：
    两条路各 10 分钟与 20 分钟时，选择型 = 10 分钟，并行型 = 6.67 分钟。

    容量直接相加。这忽略了「快的先把容量用完、剩下的只剩慢的」这种尾部，
    是长期平均速率的近似——对单件和少量需求够用，报告里要标出来。
    """
    if not segments:
        raise SupplyError("并行来源不能为空")
    if any(seg.unit_cost <= 0 for seg in segments):
        raise SupplyError(
            "并行来源里不能有零成本成员：速率是无限的，无法按速率分摊件数"
        )
    rates = [1.0 / seg.unit_cost for seg in segments]
    unit = 1.0 / sum(rates)
    # 合并后的相对标准差按**速率**加权：各成员独立时 `Var(R) = Σ rᵢ²·cvᵢ²`，
    # `cv(R) = √Var(R) / Σrᵢ`。速率相加把波动也摊薄了，这正是并行的价值。
    merged_cv = math.sqrt(
        sum((rate * seg.cv) ** 2 for rate, seg in zip(rates, segments))
    ) / sum(rates)
    names = sorted(seg.action_id for seg in segments)
    return Segment(
        action_id="race[" + "|".join(names) + "]",
        unit_cost=unit,
        capacity=sum(seg.capacity for seg in segments),
        label=label or "并行：" + "、".join(names),
        level=min(seg.level for seg in segments),
        members=tuple((seg.action_id, seg.unit_cost) for seg in segments),
        # 并行时每条路各自的一次性投入都要付，但**同名**前置只算一次
        # （同组成员的前置本来就要求一致）。
        fixed=merge_fixed(*(seg.fixed for seg in segments)),
        cv=merged_cv,
    )


@dataclass(frozen=True)
class Pruned:
    """剪枝结果。`dropped` 里带「被谁支配」，便于解释为什么绕路不见了。"""

    kept: tuple[Action, ...]
    dropped: tuple[tuple[str, str], ...]

    def as_dict(self) -> dict:
        return {
            "kept": [action.action_id for action in self.kept],
            "dropped": [{"action_id": a, "dominated_by": b} for a, b in self.dropped],
        }


def _requires_within(better: Action, worse: Action) -> bool:
    """`better` 的前置是否都不比 `worse` 更贵。

    按 `(物品, 模式)` 配对比较用量——只比物品名会把
    「要 1 个铁」与「要 3 个铁」当成一样，从而错误地判成支配。
    """
    index = {(req.item, req.mode): req.qty for req in worse.requires}
    for req in better.requires:
        qty = index.get((req.item, req.mode))
        if qty is None or qty + EPS < req.qty:
            return False
    return True


def _dominates(better: Action, worse: Action) -> bool:
    """`better` 是否支配 `worse`。

    条件五条，缺一不可：

    1. 每件更便宜（按主产物算，不含前置成本——前置看下一条）；
    2. 容量不更小；
    3. 前置不更多（逐项用量都不更大）；
    4. 一次性成本（`overhead`）不更高；
    5. 副产物不更少（否则「顺带拿到别的东西」这个好处被丢掉了）。

    第 4 条不能省：同一个方块的三种采集方式常常是「单价低但到达贵」
    对「单价高但到达便宜」（走洞穴 vs 捡地表河床），两条各有更优的需求区间，
    只比单价会把便宜那条在小需求量下正确的最优剪掉。
    """
    if better.item != worse.item:
        return False
    if better.unit_cost() > worse.unit_cost() + EPS:
        return False
    # 单次调用更贵的不能算支配。一次调用至少要付 `cost`，所以小需求量下
    # 「起步价低、单价高」的动作反而更省——袭击按波次累加就是典型：
    # 打到第 5 波（24.2 TU 换 1 只）与打完整场（36.7 TU 换 4 只）互不支配，
    # 只比单价会把前者剪掉，而整次口径拿 1 只时它才是最便宜的。
    if better.cost > worse.cost + EPS:
        return False
    # 容量按**件数**比，不按动作次数：一次出 5 件的动作做 10 次能给 50 件，
    # 按次数比会把它判给「一次出 1 件、做 10 次」的动作支配。
    if better.item_capacity() < worse.item_capacity() - EPS:
        return False
    if not _requires_within(better, worse):
        return False
    if better.overhead_total() > worse.overhead_total() + EPS:
        return False
    for name, amount in worse.side_yields.items():
        if better.yields.get(name, 0.0) + EPS < amount:
            return False
    return True


def prune(actions: Sequence[Action]) -> Pruned:
    """Pareto 剪枝：更贵 / 容量更小 / 前置更多的动作直接出局。

    这是「内部小绕路自动消失」的另一半机制：绕路往往带来额外的前置，
    结构上就被支配了，不用等数值比较。

    互支配（两条完全一样的来源）时按 `action_id` 字典序留一条，
    否则会互相把对方判掉、结果两条都没了。

    **`race` 动作不参与剪枝，双向都是。** 并行语义下「更贵」不等于「更差」——
    贵的那条仍在提供一份额外速率。按单独成本判支配会把并行收益删掉。
    但它**可以当支配者**：并行成员单独拿出来也照样比一个绕路便宜。
    """
    kept: list[Action] = []
    dropped: list[tuple[str, str]] = []
    ordered = sorted(actions, key=lambda action: action.action_id)
    for action in ordered:
        if action.race is not None:
            kept.append(action)
            continue
        dominator = None
        for other in ordered:
            if other.action_id == action.action_id:
                continue
            if not _dominates(other, action):
                continue
            if _dominates(action, other) and other.action_id > action.action_id:
                continue
            dominator = other
            break
        if dominator is None:
            kept.append(action)
        else:
            dropped.append((action.action_id, dominator.action_id))
    return Pruned(kept=tuple(kept), dropped=tuple(dropped))


def invocations_for(action: Action, demand: Mapping[str, float]) -> float:
    """联合产出：需要 `demand` 里每样东西时，这个动作要做几次。

    取 `max(n_i / yield_i)`——一次动作同时满足多个需求，不该各算一次。
    """
    if not demand:
        return 0.0
    worst = 0.0
    for item, amount in demand.items():
        if amount <= EPS:
            continue
        produced = action.yields.get(item, 0.0)
        if produced <= EPS:
            return math.inf
        worst = max(worst, amount / produced)
    return worst
