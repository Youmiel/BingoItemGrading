"""难度表：整次调用口径 + 置信度判定。

前两类断言是纯逻辑（不依赖数据），后两类跑真实快照。
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from bingo_grading import grade
from bingo_grading.case.context import CaseContext
from bingo_grading.plan import solve
from bingo_grading.plan.whole import whole_choice
from bingo_grading.sources import actions

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "data" / "stats" / "26.3"
INDEX = ROOT / "build" / "26.3" / "source_index.jsonl"

needs_data = pytest.mark.skipif(
    not SNAPSHOT.exists() or not INDEX.exists(),
    reason=f"缺少快照 {SNAPSHOT} 或来源索引 {INDEX}",
)


@pytest.fixture(scope="module")
def ctx() -> CaseContext:
    return CaseContext.load(SNAPSHOT)


# ---------------------------------------------------------------- 纯逻辑


def test_own_gaps_only_counts_the_target() -> None:
    """整图的缺口不算在目标头上：查钻石时碰到的铁块没有密度，与钻石无关。"""
    uncovered = {
        "缺方块密度（有合成替代）:minecraft:diamond_block": 1,
        "缺生物密度:minecraft:creeper": 2,
        "该表不含目标物品:minecraft:chests/simple_dungeon": 1,
        "缺每次掉落期望:minecraft:diamond": 3,
    }
    assert grade.own_gaps("minecraft:diamond", uncovered) == 3
    assert grade.own_gaps("minecraft:emerald", uncovered) == 0
    # 无害类别从这里就扣掉，不参与「图有多脏」的统计。
    assert grade.real_gaps(uncovered) == 6


def test_confidence_uses_own_gaps_and_segments() -> None:
    """一条路 + 目标自身断源 = low；来源够多且自身无缺 = high。"""
    assert grade.confidence_of(feasible=False, uncovered_own=0, segments=0) == "none"
    assert grade.confidence_of(feasible=True, uncovered_own=1, segments=9) == "low"
    assert grade.confidence_of(feasible=True, uncovered_own=0, segments=1) == "low"
    assert grade.confidence_of(feasible=True, uncovered_own=0, segments=3) == "medium"
    assert grade.confidence_of(feasible=True, uncovered_own=0, segments=4) == "high"


def test_table_aligns_with_cjk_width() -> None:
    """中文字符占两列，表格不能按字符数对齐——那样中文列会歪。"""
    rows = [
        grade.DifficultyRow(
            item="minecraft:diamond",
            score=2,
            tier=1,
            cost_tu=0.9,
            whole_cost_tu=2.88,
            route="loot:buried_treasures",
            route_label="",
            sources=10,
            uncovered_total=3,
            uncovered_own=0,
            segments=14,
            confidence="high",
        ),
        grade.DifficultyRow(
            item="minecraft:elytra",
            score=7,
            tier=3,
            cost_tu=88.66,
            whole_cost_tu=92.86,
            route="loot:end_cities",
            route_label="",
            sources=9,
            uncovered_total=1,
            uncovered_own=0,
            segments=1,
            confidence="low",
        ),
    ]
    text = grade.render_table(rows)
    lines = text.splitlines()
    assert "整次 TU" in lines[0]
    # 对齐检查：表头「整次」与数据值必须落在同一显示列上。
    header = lines[0]
    data = lines[2]
    assert "minecraft:diamond" in data
    head_at = grade._display_width(header[: header.index("整次")])
    value_at = grade._display_width(data[: data.index("2.88")])
    assert head_at == value_at, f"「整次」列表头在第 {head_at} 列，数据在第 {value_at} 列"


# ---------------------------------------------------------------- 真实数据


@needs_data
def test_whole_cost_beats_shortcut_route(ctx: CaseContext) -> None:
    """绿宝石的最优「均摊」是跑一趟试炼大厅，但拿第一颗不该按那一趟算。"""
    built = actions.build(ctx, "minecraft:emerald")
    solved = solve(built.actionset(), "minecraft:emerald", 1.0)
    cost, action_id, _, _ = grade.best_whole_cost(
        built.actionset(), solved.curves, "minecraft:emerald", 1.0
    )
    assert cost is not None
    # 均摊口径给出 ~0.6 TU，直接拿来当难度会低一个数量级。
    assert cost > solved.plan.cost
    # 整次成本与均摊应当同一量级：既不能「一趟 100 个箱子」摊成单件几分钱，
    # 也不该反过来变成跑几十趟。用倍数而不是绝对值，免得到处跟着标尺改。
    assert cost < solved.plan.cost * 30, (
        f"整次成本 {cost:.2f} vs 均摊 {solved.plan.cost:.2f}，差距过大"
    )
    assert action_id != "loot:trial_chambers", "没能绕开那一趟试炼大厅"


@needs_data
def test_whole_cost_recurses_into_inputs(ctx: CaseContext) -> None:
    """原料也按整次计价：只对目标层取整会让「买一整批原料」按均摊算。"""
    built = actions.build(ctx, "minecraft:ender_eye")
    solved = solve(built.actionset(), "minecraft:ender_eye", 1.0)
    cost, action_id, _, _ = grade.best_whole_cost(
        built.actionset(), solved.curves, "minecraft:ender_eye", 1.0
    )
    assert cost is not None
    assert action_id.startswith("craft:")
    # 末影之眼 = 末影珍珠 + 烈焰粉，两者都得整次买，所以必须高于均摊。
    #
    # 阈值不能定得太紧：整次与均摊的差距是「余数那一次」，其相对大小是
    # `1/calls`。低掉落率来源（`roam:` 打 188 只才掉 1 个）的 `calls` 很大，
    # 余数占比自然很小——实测末影之眼现在只差 5%。这里只要分清「有递归」
    # （差几个百分点）和「没递归」（两者完全相同），所以取 2%。
    assert cost > solved.plan.cost * 1.02, (
        f"整次 {cost:.2f} 与均摊 {solved.plan.cost:.2f} 几乎相同，原料层可能没递归"
    )


@needs_data
def test_whole_cost_converges_to_amortised_at_large_demand(ctx: CaseContext) -> None:
    """需求很大时整次口径向均摊收敛——两者的差距只有余数那一次。"""
    built = actions.build(ctx, "minecraft:coal")
    aset = built.actionset()
    solved = solve(aset, "minecraft:coal", 32.0)
    whole, _, _, _ = grade.best_whole_cost(aset, solved.curves, "minecraft:coal", 32.0)
    assert whole is not None
    # 32 件时不该再出现数量级的偏差（N=1 时是 10 倍）。
    # 注意只断言上界：整次可以**比均摊更便宜**，因为它会按整次重新组合来源，
    # 而均摊的 `_pick` 受容量与枚举限制未必找到同一组合
    # （实测 coal N=32：均摊走海底废墟 5.55，整次走「挖试炼废墟的煤炭块再拆开」5.26）。
    assert whole <= solved.plan.cost * 2.0, (
        f"整次 {whole:.2f} vs 均摊 {solved.plan.cost:.2f}，差距仍然过大"
    )


@needs_data
def test_atomic_routes_are_rounded_up(ctx: CaseContext) -> None:
    """跑一趟只产出一小部分需求时，必须付整趟：调用次数向上取整。"""
    built = actions.build(ctx, "minecraft:elytra")
    aset = built.actionset()
    solved = solve(aset, "minecraft:elytra", 1.0)
    cost, action_id, _, _ = grade.best_whole_cost(
        aset, solved.curves, "minecraft:elytra", 1.0
    )
    assert action_id.startswith("loot:")
    assert cost is not None
    # 末地船每处只有约 0.5 个鞘翅：一趟凑不够一整件，必须跑 2 趟。
    choice = whole_choice(aset, solved.curves, "minecraft:elytra", 1.0)
    per_trip = aset.get(choice.action_id).effective_yield("minecraft:elytra")
    assert choice.calls == math.ceil(1.0 / per_trip - 1e-9)
    assert choice.calls > 1.0, "产出不足一件时必须多于一趟"
    # **不能**断言「整次 ≥ 均摊」：整次会把同一条路线里重复的 `once` 前置
    # （`capability:the_end_access` 这类）只收一次，均摊口径的宽松递归会收两遍，
    # 所以整次可能略便宜（实测 elytra 1439 vs 1479）。只要求不出现数量级偏差。
    assert cost >= solved.plan.cost * 0.9, (
        f"整次 {cost:.2f} 比均摊 {solved.plan.cost:.2f} 低太多，取整可能失效"
    )


# ---------------------------------------------------------------- 表格输出


def _row(**overrides) -> grade.DifficultyRow:
    base = dict(
        item="minecraft:coal",
        score=2,
        cost_score=2,
        tier=1,
        cost_tu=0.61,
        whole_cost_tu=3.05,
        route="mine:cave:coal_ore",
        route_label="下洞穴挖 1 个 minecraft:coal_ore（每区块暴露 0.89 块）",
        sources=134,
        uncovered_total=5,
        uncovered_own=0,
        segments=18,
        confidence="high",
        notes=(),
    )
    return grade.DifficultyRow(**{**base, **overrides})


def test_markdown_table_has_one_row_per_item() -> None:
    """Markdown 表的行数 = 物品数 + 表头两行，且「说明」列去掉参数尾巴。"""
    text = grade.render_markdown(
        [
            _row(),
            _row(item="minecraft:diamond", route="loot:x", route_label=""),
            _row(item="minecraft:abandoned_camp_map", score=None, tier=None,
                     whole_cost_tu=None, cost_tu=None, confidence="none",
                     route="", route_label=""),
        ]
    )
    lines = text.splitlines()
    assert len(lines) == 2 + 3
    assert lines[0].startswith("| 物品 |")
    assert "下洞穴挖 1 个 minecraft:coal_ore" in lines[2]
    assert "（每区块暴露" not in lines[2], "参数尾巴没截掉"
    # 不可得的物品用 `-`，不能写成 0 或 None。
    assert lines[4].endswith("| - | - |")


def test_csv_table_keeps_raw_numbers() -> None:
    """CSV 用原始数值（不四舍五入到整数），首行是列名。"""
    text = grade.render_csv([_row()])
    lines = text.splitlines()
    assert not text.startswith("\ufeff"), "BOM 由写文件那一层加，不在文本里"
    assert lines[0].startswith("item,score,cost_score,tier,whole_cost_tu,p95_cost_tu,tail_score")
    # `score` 取两维最大，`cost_score` 留原始均摊分；没有方差信息时两者相同。
    assert "minecraft:coal,2,2,1,3.05,,,0.61" in lines[1]


def test_csv_file_carries_utf8_bom(tmp_path: Path) -> None:
    """CSV 落盘带 UTF-8 BOM（Excel 双击打开靠它认编码），Markdown 不带。"""
    csv_path = tmp_path / "b.csv"
    assert grade.render_table_file([_row()], str(csv_path)) == "csv"
    raw = csv_path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert raw.decode("utf-8-sig").startswith("item,score")

    md = tmp_path / "a.md"
    grade.render_table_file([_row()], str(md))
    assert not md.read_bytes().startswith(b"\xef\xbb\xbf")


def test_table_file_picks_format_by_suffix(tmp_path: Path) -> None:
    """后缀决定格式；目录不存在时自己建。"""
    md = tmp_path / "sub" / "a.md"
    csv_path = tmp_path / "b.csv"
    assert grade.render_table_file([_row()], str(md)) == "markdown"
    assert md.read_text(encoding="utf-8").startswith("| 物品 |")
    assert grade.render_table_file([_row()], str(csv_path)) == "csv"
    assert csv_path.read_text(encoding="utf-8-sig").startswith("item,score")
    assert grade.render_table_file([_row()], str(tmp_path / "c.txt")) == "text"
