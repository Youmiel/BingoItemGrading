"""Bingo 难度参考：把一批物品折算成同一张难度表。

## 为什么需要这一层

`bg plan` 一次回答「这一件要多久」。Bingo 的用法是反过来的：
一张卡有几十件物品，需要的是**横向可比的难度表**，以及「哪些数字不可信」。

所以这里只做三件事，不引入任何新的成本口径：

1. 对每个物品跑一遍既有的自动拼图与求解，取 `plan.cost` → `score`；
2. 把「这条分数有多可信」显式写出来（缺口、来源条数、曲线段数）；
3. 汇总成一张卡的总难度。

评分口径不在这里定义，沿用 `bingo_grading.units.score_from_cost`
（`score = clamp(round(log2(cost / SCORE_FLOOR) × SCORE_STEP), 0, 30)`，
每 1 分 = 成本 ×√2，零点 0.1 TU）。

## 置信度是怎么判的

不看「整张图报了多少缺口」，而看两件事，都能从数据自证：

1. **目标自身的来源有没有断**。`uncovered` 是整个动作用图的缺口，里面混着
   大量与目标无关的条目——查钻石时递归碰到的 `iron_block` 没有密度，
   也会计一笔。用总数判置信度就永远是 `low`，这个字段也就没用了。
   所以只数 `原因:对象` 里**对象等于目标物品**的那些。
2. **供给曲线有几段**。只有一段意味着「只有一条路」，那条路错了整件就错。

这不是统计置信度，是**模型自评的可信度**。
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, replace
from pathlib import Path

from bingo_grading import units
from bingo_grading.plan.actions import MODE_PER_USE, is_discrete
from bingo_grading.plan.supply import TAIL_Z
from bingo_grading.plan.whole import WholeChoice, dedupe_once_costs, whole_choice

# `uncovered` 里这些不算缺口：路线另有来源，只是本条记录没接上。
#
# - `缺方块密度（有合成替代）`：铁块/金块这类 9 合 1 的方块，合成路线已经在图上；
# - `模板物品绑定（R4，无表名）`：上游只说了「模板里某容器装着它」，
#   没有战利品表名，属于模板解析的范畴。
HARMLESS_PREFIXES = (
    "缺方块密度（有合成替代）",
    "模板物品绑定（R4，无表名）",
)

# 单条来源就该被警告：一条路错了，这件物品的分数就整体错。
LOW_SEGMENT_COUNT = 1
# 段数到多少算「来源够多」。三档以内说明选择余地很窄。
NARROW_SEGMENT_COUNT = 3
# 清单超过这个件数就改用整图求解：单件走目标子图约 1.5 秒，整图一次约 6 秒。
JOINT_THRESHOLD = 4

# 尾部成本的分位系数由 `plan/supply.py::TAIL_Z` 提供（文件头已 import）：
# 均摊曲线与整次口径必须用同一个数，否则两者的差就不是纯粹的口径差了。


def best_whole_cost(
    actionset,
    curves,
    item: str,
    demand: float,
    *,
    seen: frozenset[str] = frozenset(),
    memo: dict | None = None,
    actions_by_id: dict | None = None,
) -> tuple[float | None, str, str, float]:
    """求**整次调用**口径下拿到 `demand` 件的最低成本。

    与 `bg plan` 共用 `plan/whole.py` 的同一份递归，两处不会对同一件事给出
    两个答案：连续松弛允许「调用 0.54 次」，而跑一趟沉船要么进要么不进。

    返回 `(成本, 动作 id, 说明, 方差)`；不可达时成本为 `None`。
    """
    choice = whole_choice(
        actionset,
        curves,
        item,
        demand,
        seen=seen,
        memo=memo,
        actions_by_id=actions_by_id,
    )
    return (choice.cost, choice.action_id, choice.label, choice.variance)


@dataclass(frozen=True)
class BundleStep:
    """组合方案里的一步：某个动作调用几次、顺带解决了哪几件目标。"""

    action_id: str
    calls: float
    covers: tuple[str, ...]
    cost: float


@dataclass(frozen=True)
class Bundle:
    """一次买齐多件物品的方案。"""

    cost: float | None
    steps: tuple[BundleStep, ...]

    @property
    def feasible(self) -> bool:
        return self.cost is not None

    def as_dict(self) -> dict:
        # 键名与 `DifficultyRow.as_dict()` 统一：`cost_tu` 只表示均摊口径，
        # 整次口径一律叫 `whole_cost_tu`。组合方案没有均摊版本（它只回答
        # 「真跑一遍多贵」），所以这里只有 `whole_cost_tu`。
        return {
            "whole_cost_tu": None if self.cost is None else round(self.cost, 4),
            "steps": [
                {
                    "action_id": step.action_id,
                    "calls": step.calls,
                    "covers": list(step.covers),
                    "whole_cost_tu": round(step.cost, 4),
                }
                for step in self.steps
            ],
        }


def best_bundle(
    actionset,
    curves,
    demands: dict[str, float],
    *,
    seen: frozenset[str] = frozenset(),
) -> Bundle:
    """**一次买齐** `demands` 里所有物品的最便宜方案。

    与 `best_whole_cost()` 的分工：那个一次只问一件，几件加在一起会重复付行程，
    而且会把「同一趟顺手拿到的东西」算成各自跑一趟。实测 `loot:buried_treasures`
    一趟同时给钻石 0.96、金锭 4.48、铁锭 8.95、绿宝石 3.86……所以
    「钻石 + 金锭 + 铁锭」这三格跑一趟就够，分别算会高估两三倍。

    算法：枚举「这一趟由哪个动作负责」——一个动作能同时供给它 `yields` 里的
    所有目标，调用次数取 `max(⌈需求ᵢ ÷ 单次产出ᵢ⌉)`（跑一趟就够所有物品，
    不必各跑一次），剩下的目标递归求。这是**集合覆盖**的贪心版：
    目标件数少（Bingo 一格 1–3 件）时递归深度很浅，穷举得起。

    容量有限的动作用 `capacity` 卡住：一趟给不够的目标不会被这条动作认领。

    **前置的定价与 `once` 去重全部交给 `plan/whole.py`**：每步的 `requires` 走
    `whole_choice`（整次取整 + 递归），最后对整棵树调一次 `dedupe_once_costs`。
    这里只负责「一个动作同时供给多件」这层语义——那才是 `whole_choice`（单目标）
    不管的部分。此前这里自带一套 `math.ceil` 和 `paid` 集合（还要把它并进
    `memo` 键、靠 `top` 只扩一层来防爆炸），两套规则会在「共享发生在第二层以下」
    时给出不同答案。现在同样的树只按一个规则算。
    """
    root = _solve_bundle(actionset, curves, demands, seen=seen, memo={})
    if root.cost is None:
        return Bundle(None, ())
    root = dedupe_once_costs(root)
    steps = tuple(
        BundleStep(
            action_id=node.payload[0],
            calls=node.payload[1],
            covers=node.payload[2],
            cost=node.own_cost,
        )
        for node in _step_nodes(root)
    )
    return Bundle(root.cost, steps)


def _step_nodes(node: WholeChoice):
    """按前序取出「一次买齐」的每一步（`payload` 是那一步的元数据）。"""
    if node.payload:
        yield node
    for child in node.children:
        yield from _step_nodes(child)


def _solve_bundle(
    actionset,
    curves,
    demands: dict[str, float],
    *,
    seen: frozenset[str],
    memo: dict,
) -> WholeChoice:
    """枚举「这一趟由谁负责」的整次口径递归（不去重，见 `best_bundle`）。

    返回的节点用 `plan/whole.py` 的 `WholeChoice`：`own_cost` 是本步自身
    （`overhead + cost × calls`），`children` 是这一前置 + 剩下的目标。
    `payload` 记这一步的 `(action_id, calls, covers)`。
    """
    key = tuple(
        sorted(
            (item, round(amount, 6))
            for item, amount in demands.items()
            if amount > 0
        )
    )
    if key in memo:
        return memo[key]
    remaining = {item: amount for item, amount in demands.items() if amount > 0}
    if not remaining:
        return WholeChoice(item="<空>", need=1.0, cost=0.0, own_cost=0.0, tree=True)

    best: WholeChoice | None = None
    for action in actionset.actions:
        covered: list[str] = []
        calls = 0.0
        for item, amount in remaining.items():
            effective = action.effective_yield(item)
            if effective <= 0:
                continue
            # 产量上限不够一件都凑不齐的，这条动作认领不了。
            if math.isfinite(action.capacity) and action.capacity * effective < amount:
                continue
            need = amount / effective
            if is_discrete(action.action_id):
                need = math.ceil(need - 1e-9)
            else:
                need = max(need, 1e-9)
            covered.append(item)
            calls = max(calls, need)
        if not covered or calls <= 0:
            continue
        if action.action_id in seen:
            # 回到已经用过的动作：这条分支会原地打转。
            continue
        own = sum(amount for _, amount in action.overhead) + action.cost * calls
        total = own
        children: list[WholeChoice] = []
        for requirement in action.requires:
            once = requirement.mode != MODE_PER_USE
            need = requirement.qty if once else requirement.qty * calls
            sub = whole_choice(
                actionset,
                curves,
                requirement.item,
                need,
                # 不传外层 `seen`：那边的集合装的是**动作 id**（防「这条动作再用一次」
                # 的原地打转），`whole_choice` 的 `seen` 装的是**物品名**。混着传会让
                # 某个名字相同的物品被误判成环；前置链的环由 `whole_choice` 自己管。
                # 必须带子树：整树去重要看得见前置**内部**的 `once`
                # （`capability:minecraft:nether_complexes` 的成本里含
                # `capability:nether_access`，另一条链也要它）。
                want_children=True,
            )
            if sub.cost is None:
                total = None
                break
            total += sub.cost
            # 记下付费模式：整树去重只对 `once` 生效。
            children.append(replace(sub, once=once))
        if total is None:
            continue
        rest = {item: amount for item, amount in remaining.items() if item not in covered}
        if rest:
            tail = _solve_bundle(
                actionset,
                curves,
                rest,
                seen=seen | {action.action_id},
                memo=memo,
            )
            if tail.cost is None:
                continue
            total += tail.cost
            children.append(tail)
        node = WholeChoice(
            item="<买齐>",
            need=1.0,
            cost=total,
            own_cost=own,
            children=tuple(children),
            tree=True,
            payload=(action.action_id, calls, tuple(sorted(covered))),
        )
        if best is None or total < best.cost:
            best = node
    memo[key] = (
        best
        if best is not None
        else WholeChoice(item="<买齐>", need=1.0, cost=None, blocked="missing")
    )
    return memo[key]


@dataclass(frozen=True)
class DifficultyRow:
    """一件物品的难度评分，以及这个评分的可信度。"""

    item: str
    # 最终分数：`max(成本分, 尾部分)`。两个维度并列输出，不合并成一个连续值。
    score: int | None
    tier: int | None
    cost_tu: float | None
    whole_cost_tu: float | None
    route: str
    route_label: str
    sources: int
    uncovered_total: int
    uncovered_own: int
    segments: int
    confidence: str
    # 尾部口径：`whole_cost_tu + TAIL_Z · sd`。`None` = 拿不到方差（所有来源都
    # 没标 `yield_cv`）或不可行；与 `whole_cost_tu` 同口径，两者可直接比。
    p95_cost_tu: float | None = None
    # 两个维度的分数：`cost_score` 只看平均成本，`tail_score` 只看 p95。
    # `score` 取两者最大值；这里留存原始值，否则看不出分数是被尾部抬上去的。
    cost_score: int | None = None
    tail_score: int | None = None
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "item": self.item,
            "score": self.score,
            "tier": self.tier,
            # 引擎口径（连续松弛）：批量看用这个。
            "cost_tu": None if self.cost_tu is None else round(self.cost_tu, 4),
            # 难度口径（整次调用）：`score` 就建在它上面。
            "whole_cost_tu": (
                None if self.whole_cost_tu is None else round(self.whole_cost_tu, 4)
            ),
            "route": self.route,
            "route_label": self.route_label,
            "sources": self.sources,
            "uncovered_total": self.uncovered_total,
            "uncovered_own": self.uncovered_own,
            "segments": self.segments,
            "confidence": self.confidence,
            "p95_cost_tu": (
                None if self.p95_cost_tu is None else round(self.p95_cost_tu, 4)
            ),
            "cost_score": self.cost_score,
            "tail_score": self.tail_score,
            "notes": list(self.notes),
        }


def own_gaps(item: str, uncovered: dict[str, int]) -> int:
    """只数**目标物品自己**的缺口。

    键的格式约定（2026-11-22 统一）：

    | 形态 | 含义 | 计入本函数 |
    |---|---|---|
    | `原因:item` | 物品级缺口，`item` 就是缺的那件 | ✅ |
    | `原因(subject):item` | 物品级缺口，括号里是主体（结构/生物/表） | ✅ |
    | `原因:subject` | **结构级**缺口，影响一整类物品，不归到某一件 | ❌ |

    `partition(":")` 只切第一个冒号，所以前两种取到的正好是 `item`
    （`item` 自己带冒号无妨）。**括号里的主体必须是短名、不能含冒号**——
    写成 `[minecraft:cow]` 会把第一个冒号切在括号里，取到的就不是物品名了。

    结构级的例子：`缺结构密度:minecraft:ancient_city`——那是「古代城市缺密度数据」，
    会让该结构下的所有物品都少一条来源，记到某一物品头上反而是错的。
    """
    total = 0
    for reason, count in uncovered.items():
        _, _, subject = reason.partition(":")
        if subject == item:
            total += count
    return total


def real_gaps(uncovered: dict[str, int]) -> int:
    """扣掉无害类别之后的缺口条数（仅作信息展示，不参与置信度）。"""
    return sum(
        count
        for reason, count in uncovered.items()
        if not reason.startswith(HARMLESS_PREFIXES)
    )


def confidence_of(*, feasible: bool, uncovered_own: int, segments: int) -> str:
    """模型自评的可信度：`none` / `low` / `medium` / `high`。

    `none` 表示连可行路线都没有——那不是「简单」或「很难」，
    是「这件物品现在算不出来」，必须与低分区分开。
    """
    if not feasible:
        return "none"
    if uncovered_own > 0 or segments <= LOW_SEGMENT_COUNT:
        return "low"
    if segments <= NARROW_SEGMENT_COUNT:
        return "medium"
    return "high"


def row_of(
    *,
    item: str,
    cost: float | None,
    whole_cost: float | None,
    feasible: bool,
    route: str,
    route_label: str,
    sources: int,
    uncovered: dict[str, int],
    segments: int,
    demand: float = 1.0,
    variance: float = 0.0,
) -> DifficultyRow:
    """把一次求解结果整理成一行难度。

    `score` 用 `whole_cost`（整次调用）而不是引擎的 `cost`（连续松弛）：
    难度问的是「凑够 `demand` 件要多久」。`demand = 1` 时两者差别最大，
    `demand` 很大时向均摊收敛（差一个 `1 - 1/calls` 量级）。

    `variance` 是同一个方案的**成本方差**（由 `best_whole_cost` 同步累加）。
    它只用来算并列的 `p95_cost_tu` 与 `tail_score`，不影响两个维度各自的定义：
    `cost_score` 只看均值、`tail_score` 只看 p95，最终 `score = max(两者)`。
    """
    own = own_gaps(item, uncovered)
    notes: list[str] = []
    p95: float | None = None
    cost_score: int | None = None
    tail_score: int | None = None
    if not feasible or whole_cost is None:
        notes.append("算不出可行路线：来源全部缺失或前置不可达")
    else:
        cost_score = units.score_from_cost(whole_cost)
        # 正态单侧 95%；没有方差信息时退化成均值本身。
        p95 = whole_cost + TAIL_Z * math.sqrt(max(0.0, variance))
        tail_score = units.score_from_cost(p95)
        if cost_score >= units.SCORE_MAX:
            notes.append("分数已封顶，实际可能更难")
        elif tail_score > cost_score:
            notes.append(
                f"分数按尾部计：均摊 {whole_cost:.2f} TU → p95 {p95:.2f} TU"
            )
        if segments <= LOW_SEGMENT_COUNT:
            notes.append("只有一条来源，该来源一旦有误整件都会错")
        if cost is not None and whole_cost > cost * 1.5:
            notes.append(
                f"要凑够 {demand:g} 件得跑整趟（均摊 {cost:.2f} TU，实际 {whole_cost:.2f} TU）"
            )
    if own:
        notes.append(f"该物品自己还有 {own} 条来源没接进模型")
    final_score = (
        None
        if cost_score is None
        else max(cost_score, cost_score if tail_score is None else tail_score)
    )
    # `tier` 跟分数同源：分数是按哪个成本抬上去的，档位就按哪个成本算。
    # 两个刻度不同（`TIER_BOUNDS` vs `SCORE_FLOOR/STEP`），不能各自取 max 混搭。
    tier_cost = whole_cost
    if tail_score is not None and cost_score is not None and tail_score > cost_score:
        tier_cost = p95
    return DifficultyRow(
        item=item,
        score=final_score,
        tier=None if tier_cost is None else units.tier_of(tier_cost),
        cost_tu=None if not feasible or cost is None else cost,
        whole_cost_tu=None if not feasible or whole_cost is None else whole_cost,
        route=route,
        route_label=route_label,
        sources=sources,
        uncovered_total=sum(uncovered.values()),
        uncovered_own=own,
        segments=segments,
        confidence=confidence_of(
            feasible=feasible, uncovered_own=own, segments=segments
        ),
        p95_cost_tu=p95,
        cost_score=cost_score,
        tail_score=tail_score,
        notes=tuple(notes),
    )


def summarize(rows: list[DifficultyRow]) -> dict:
    """一张卡的难度汇总。

    `total_score` 是各行分数之和，用来看卡的**总体量**；同时给出 `hardest`，
    因为 Bingo 的节奏通常由最难那件决定。两者并列，不合并成一个数。
    """
    scored = [row for row in rows if row.score is not None]
    hardest = max(scored, key=lambda row: row.score or 0) if scored else None
    unknown = [row.item for row in rows if row.score is None]
    shaky = [row.item for row in rows if row.confidence in ("low", "none")]
    return {
        "items": len(rows),
        "scored": len(scored),
        "total_score": sum(row.score or 0 for row in scored),
        "hardest": None
        if hardest is None
        else {
            "item": hardest.item,
            "score": hardest.score,
            "whole_cost_tu": round(hardest.whole_cost_tu or 0.0, 4),
        },
        "unknown": unknown,
        "low_confidence": shaky,
    }


def render_table(rows: list[DifficultyRow]) -> str:
    """对齐的文本表。列宽按内容算，不手抄空格数。"""
    header = (
        "物品",
        "分数",
        "档",
        "整次 TU",
        "尾部 TU",
        "均摊 TU",
        "可信度",
        "主要路线",
    )
    body = [
        (
            row.item,
            "-" if row.score is None else str(row.score),
            "-" if row.tier is None else str(row.tier),
            "-" if row.whole_cost_tu is None else f"{row.whole_cost_tu:.2f}",
            "-" if row.p95_cost_tu is None else f"{row.p95_cost_tu:.2f}",
            "-" if row.cost_tu is None else f"{row.cost_tu:.2f}",
            row.confidence,
            row.route,
        )
        for row in rows
    ]
    widths = [
        max(_display_width(line[index]) for line in [header, *body])
        for index in range(len(header))
    ]
    lines = []
    for line in [header, *body]:
        cells = []
        for index, cell in enumerate(line):
            pad = widths[index] - _display_width(cell)
            cells.append(cell + " " * pad)
        lines.append("  ".join(cells).rstrip())
    lines.insert(1, "  ".join("-" * width for width in widths))
    return "\n".join(lines)


def render_markdown(rows: list[DifficultyRow]) -> str:
    """给人看的 Markdown 表：多一列「说明」，路线 id 之外能看到在干什么。

    全库 1414 行在编辑器里可以直接预览或排序，比对齐文本表好用。
    `route_label` 里的配方路径很长，截在第一个「（」之前。
    """
    lines = [
        "| 物品 | 分数 | 档 | 整次 TU | 尾部 TU | 均摊 TU | 可信度 | 主要路线 | 说明 |",
        "|---|---:|---:|---:|---:|---:|---|---|---|",
    ]
    for row in rows:
        # 去掉「（…参数…）」尾巴，并把多余空白压成一个空格。
        label = " ".join(row.route_label.split("（", 1)[0].split())
        lines.append(
            "| "
            + " | ".join(
                (
                    row.item,
                    "-" if row.score is None else str(row.score),
                    "-" if row.tier is None else str(row.tier),
                    "-" if row.whole_cost_tu is None else f"{row.whole_cost_tu:.2f}",
                    "-" if row.p95_cost_tu is None else f"{row.p95_cost_tu:.2f}",
                    "-" if row.cost_tu is None else f"{row.cost_tu:.2f}",
                    row.confidence,
                    f"`{row.route}`" if row.route else "-",
                    label.replace("|", "\\|") or "-",
                )
            )
            + " |"
        )
    return "\n".join(lines)


def render_csv(rows: list[DifficultyRow]) -> str:
    """CSV 文本（**不含** BOM）：给表格软件用的原始数值。

    BOM 由写入端按后缀加（见 `render_table_file`），这样这里返回的仍是普通
    字符串，内联输出与测试都不用处理前导 `\ufeff`。
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        (
            "item",
            "score",
            "cost_score",
            "tier",
            "whole_cost_tu",
            "p95_cost_tu",
            "tail_score",
            "cost_tu",
            "confidence",
            "route",
            "route_label",
            "uncovered_own",
            "uncovered_total",
            "notes",
        )
    )
    for row in rows:
        writer.writerow(
            (
                row.item,
                "" if row.score is None else row.score,
                "" if row.cost_score is None else row.cost_score,
                "" if row.tier is None else row.tier,
                "" if row.whole_cost_tu is None else round(row.whole_cost_tu, 4),
                "" if row.p95_cost_tu is None else round(row.p95_cost_tu, 4),
                "" if row.tail_score is None else row.tail_score,
                "" if row.cost_tu is None else round(row.cost_tu, 4),
                row.confidence,
                row.route,
                row.route_label,
                row.uncovered_own,
                row.uncovered_total,
                "；".join(row.notes),
            )
        )
    return buffer.getvalue()


def render_table_file(rows: list[DifficultyRow], path: str) -> str:
    """按后缀选格式写表格，返回实际格式名。

    `.md` / `.markdown` → Markdown，`.csv` → CSV，其余 → 对齐文本表。

    CSV 额外写 **UTF-8 BOM**：Excel 双击打开时靠它认编码，没有 BOM 就把中文
    列（路线、说明）显示成乱码。Markdown 不写——BOM 会让 diff 首行出现隐形字符。
    """
    suffix = Path(path).suffix.lower()
    if suffix in (".md", ".markdown"):
        text, kind = render_markdown(rows), "markdown"
    elif suffix == ".csv":
        text, kind = render_csv(rows), "csv"
    else:
        text, kind = render_table(rows), "text"
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        text + "\n", encoding="utf-8-sig" if kind == "csv" else "utf-8"
    )
    return kind


def _display_width(text: str) -> int:
    """终端显示宽度：CJK 与全角标点占两列。"""
    width = 0
    for char in text:
        width += 2 if _is_wide(char) else 1
    return width


def _is_wide(char: str) -> bool:
    code = ord(char)
    return (
        0x1100 <= code <= 0x115F
        or 0x2E80 <= code <= 0xA4CF
        or 0xAC00 <= code <= 0xD7A3
        or 0xF900 <= code <= 0xFAFF
        or 0xFE30 <= code <= 0xFE6F
        or 0xFF00 <= code <= 0xFF60
        or 0xFFE0 <= code <= 0xFFE6
    )
