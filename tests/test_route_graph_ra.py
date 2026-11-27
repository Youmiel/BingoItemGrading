"""RA 风格路线图：item / rule 二分、红色锚点、环剪断、落盘。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from bingo_grading import route_graph_ra
from bingo_grading.plan import MODE_PER_USE, Action, ActionSet, Requirement, solve
from bingo_grading.plan.explain import PathTree, Step, rank_paths
from bingo_grading.route_graph_ra.colors import (
    BEST_COLOR,
    HIGHLIGHT_COLOR,
    WORST_COLOR,
)


def _step(
    item: str,
    action_id: str,
    *,
    cost: float = 1.0,
    need: float = 1.0,
    children: tuple[Step, ...] = (),
    note: str = "",
    fixed: tuple[tuple[str, float], ...] = (),
    own_unit_cost: float = 0.0,
) -> Step:
    return Step(
        item=item,
        action_id=action_id,
        unit_cost=cost,
        need=need,
        calls=need,
        subtotal=cost * need,
        children=children,
        note=note,
        fixed=fixed,
        own_unit_cost=own_unit_cost,
    )


def _tree(rank: int, total: float, root: Step) -> PathTree:
    return PathTree(
        item="minecraft:x",
        rank=rank,
        unit_cost=root.unit_cost,
        rank_gap=total - 1.0,
        root=root,
        total_cost=total,
        need=1.0,
    )


def test_items_and_rules_are_separate_nodes() -> None:
    """物品是矩形节点、动作是菱形节点，边一律「来源 → 产物」。"""
    graph = route_graph_ra.to_path_graph(
        [_tree(1, 4.0, _step("minecraft:x", "smelt:x", children=(_step("minecraft:ore", "mine:ore"),)))],
        "minecraft:x",
    )
    assert graph.nodes["item::minecraft:x"]["sort"] == "item"
    assert graph.nodes["item::minecraft:ore"]["sort"] == "item"
    assert graph.nodes["rule::smelt:x"]["sort"] == "rule"
    assert graph.nodes["rule::smelt:x"]["html"] is True
    assert graph.target == "item::minecraft:x"
    assert ("rule::smelt:x", "item::minecraft:x", "yield:smelt:x") in graph.edges
    assert ("item::minecraft:ore", "rule::smelt:x", "need:mine:ore") in graph.edges
    assert ("rule::mine:ore", "item::minecraft:ore", "yield:mine:ore") in graph.edges


def test_rule_label_keeps_cost_and_quantity() -> None:
    """动作节点保留 `单价 TU × 需要量`，不写「单价:」占地方。"""
    graph = route_graph_ra.to_path_graph(
        [_tree(1, 4.0, _step("minecraft:x", "smelt:x", cost=2.12, need=3))],
        "minecraft:x",
    )
    label = graph.nodes["rule::smelt:x"]["label"]
    assert "2.12 TU ×3" in label
    assert "单价" not in label
    assert "CELLPADDING=\"3\"" in label


def test_rule_label_shows_fixed_cost() -> None:
    """固定成本必须上图，且与单价同一行：只写单价会让「单价低但固定贵」的来源看着更便宜。"""
    step = _step(
        "minecraft:x",
        "cast:x",
        cost=0.14,
        need=3.0,
        fixed=(("capability:lava_access", 48.4),),
    )
    graph = route_graph_ra.to_path_graph([_tree(1, 49.0, step)], "minecraft:x")
    node = graph.nodes["rule::cast:x"]
    assert "0.14 TU ×3 + 48.40 固定" in node["label"]
    assert "lava_access 48.4" in node["tooltip"]


def test_tooltip_explains_how_the_numbers_add_up() -> None:
    """悬停要能看出：单价里折进了哪些前置、固定成本合并掉了多少。"""
    blaze = _step(
        "minecraft:blaze_powder", "crush", cost=27.5, need=12.0,
        fixed=(("capability:nether", 155.27),),
    )
    pearl = _step(
        "minecraft:ender_pearl", "roam", cost=13.19, need=12.0,
        fixed=(("capability:nether", 74.01),),
    )
    root = _step(
        "minecraft:ender_eye",
        "craft",
        cost=45.69,
        need=12.0,
        children=(blaze, pearl),
        fixed=(("capability:nether", 155.27),),
        own_unit_cost=5.0,
    )
    graph = route_graph_ra.to_path_graph([_tree(1, 703.55, root)], "minecraft:x")
    tooltip = graph.nodes["rule::craft"]["tooltip"]
    # 45.69 = 5.00 自身 + 27.50×1 + 13.19×1（每个末影之眼各要 1 个烈焰粉、1 个珍珠）
    assert "单价 = 本动作 5.00 + blaze_powder 27.50×1 + ender_pearl 13.19×1" in tooltip
    # 前置固定 155.27 + 74.01 = 229.28，同名合并后 155.27
    assert "前置合计 229.28" in tooltip
    assert "同名只算一次" in tooltip
    # 图上已经写过的内容不再重复
    assert "本行" not in tooltip
    assert "整次（含全部前置）" not in tooltip


def test_once_child_is_not_folded_into_unit_price() -> None:
    """once 前置整笔进固定成本，不能出现在「单价构成」里（否则会算出负数）。"""
    state = _step("capability:state", "make_state", cost=341.9)
    root = _step(
        "minecraft:x",
        "use",
        cost=23.1,
        children=(state,),
        fixed=(("capability:state", 341.9),),
        own_unit_cost=23.1,
    )
    root = replace(root, children=(replace(state, once=True),))
    graph = route_graph_ra.to_path_graph([_tree(1, 365.0, root)], "minecraft:x")
    tooltip = graph.nodes["rule::use"]["tooltip"]
    # 这笔一次性开销已逐笔列在固定行里，不再单起一行重复。
    assert "固定 = state 341.90" in tooltip
    assert "一次性前置" not in tooltip
    assert "本动作 -" not in tooltip


def test_once_child_without_amount_is_still_named() -> None:
    """once 前置若没在固定成本里占钱（别处已付），仍要报出名字，否则读者以为它免费。"""
    state = _step("capability:state", "make_state", cost=0.0)
    root = _step(
        "minecraft:x",
        "use",
        cost=23.1,
        children=(replace(state, once=True),),
        own_unit_cost=23.1,
    )
    graph = route_graph_ra.to_path_graph([_tree(1, 23.1, root)], "minecraft:x")
    tooltip = graph.nodes["rule::use"]["tooltip"]
    assert "一次性前置：capability:state" in tooltip


def test_label_shows_whole_cost() -> None:
    """等式自洽：`单价 × 需要量 + 固定 = 整次`，整次等于 `PathTree.total_cost`。"""
    child = _step(
        "minecraft:p",
        "p1",
        cost=10.0,
        fixed=(("capability:shared", 5.0),),
    )
    # 真实数据里父节点的 unit_cost 已含子节点的边际，这里同样构造：11 = 自身 1 + 子 10
    root = _step(
        "minecraft:x",
        "make",
        cost=11.0,
        need=2.0,
        children=(child,),
        fixed=(("capability:shared", 5.0),),
    )
    graph = route_graph_ra.to_path_graph([_tree(1, 27.0, root)], "minecraft:x")
    make = graph.nodes["rule::make"]
    assert "11.00 TU ×2 + 5.00 固定 = 27.00 整次" in make["label"]
    # 叶子节点自身就是全部（10 + 5），不另写「整次」
    assert "10.00 TU ×1 + 5.00 固定" in graph.nodes["rule::p1"]["label"]
    assert "整次" not in graph.nodes["rule::p1"]["label"]


def test_fractional_need_shows_as_reciprocal() -> None:
    """一次产出多件时，前置的 `×0.111` 写成 `×1/9`，图上才读得懂。"""
    root = _step(
        "minecraft:x",
        "craft:x",
        children=(_step("minecraft:block", "craft:block", need=1 / 9),),
    )
    graph = route_graph_ra.to_path_graph([_tree(1, 4.0, root)], "minecraft:x")
    assert graph.edges[("item::minecraft:block", "rule::craft:x", "need:craft:block")][0] == "×1/9"
    assert "×1/9" in graph.nodes["rule::craft:block"]["label"]


def test_depth_cut_note_says_cost_is_included() -> None:
    """深度截断的备注要说清「成本已计入」，而不是只写「未展开」。"""
    graph = route_graph_ra.to_path_graph(
        [_tree(1, 4.0, _step("minecraft:x", "mine:x", note="递归深度用尽，下游未展开"))],
        "minecraft:x",
    )
    label = graph.nodes["rule::mine:x"]["label"]
    assert "成本已计入" in label
    assert "未展开" not in label


def test_best_candidate_is_highlighted() -> None:
    """target 的多候选里只有最便宜的标红，其余走浓淡。"""
    trees = [
        _tree(1, 10.0, _step("minecraft:x", "mine:ore")),
        _tree(2, 20.0, _step("minecraft:x", "loot:chest")),
    ]
    graph = route_graph_ra.to_path_graph(trees, "minecraft:x")
    best = ("rule::mine:ore", "item::minecraft:x", "yield:mine:ore")
    other = ("rule::loot:chest", "item::minecraft:x", "yield:loot:chest")
    assert graph.edge_colors[best] == HIGHLIGHT_COLOR
    assert graph.edge_colors[other] == WORST_COLOR
    assert graph.edges[best][0] == "10.0 TU"


def test_only_one_highlight_among_many_candidates() -> None:
    trees = [
        _tree(1, 10.0, _step("minecraft:x", "a")),
        _tree(2, 10.5, _step("minecraft:x", "b")),
        _tree(3, 30.0, _step("minecraft:x", "c")),
    ]
    graph = route_graph_ra.to_path_graph(trees, "minecraft:x")
    reds = [key for key, color in graph.edge_colors.items() if color == HIGHLIGHT_COLOR]
    assert reds == [("rule::a", "item::minecraft:x", "yield:a")]


def test_action_preconditions_are_not_highlighted() -> None:
    """一个动作的多个前置是 AND，不能挑其中一条单独标红——它们同色。"""
    root = _step(
        "minecraft:x",
        "loot:end_cities",
        children=(_step("minecraft:a", "travel:a"), _step("minecraft:b", "travel:b")),
    )
    graph = route_graph_ra.to_path_graph([_tree(1, 10.0, root)], "minecraft:x")
    first = graph.edge_colors[("item::minecraft:a", "rule::loot:end_cities", "need:travel:a")]
    second = graph.edge_colors[("item::minecraft:b", "rule::loot:end_cities", "need:travel:b")]
    assert first == second
    # 两条都是候选意义上的最优（同一棵树），不存在「只红一条」的假决策
    assert HIGHLIGHT_COLOR not in {first, second} or first == second


def test_competing_candidates_still_only_one_red() -> None:
    """多候选时红色仍只给成本最低的那条，其余走浓淡。"""
    trees = [
        _tree(1, 10.0, _step("minecraft:x", "a")),
        _tree(2, 30.0, _step("minecraft:x", "b")),
    ]
    graph = route_graph_ra.to_path_graph(trees, "minecraft:x")
    assert graph.edge_colors[("rule::a", "item::minecraft:x", "yield:a")] == HIGHLIGHT_COLOR
    assert graph.edge_colors[("rule::b", "item::minecraft:x", "yield:b")] != HIGHLIGHT_COLOR


def test_cycle_child_becomes_a_dashed_back_edge() -> None:
    """路径上已经出现过的前置画成不参与层级的虚线回边：节点不重建，但关系看得见。"""
    root = _step(
        "minecraft:x",
        "craft:block",
        children=(
            _step(
                "minecraft:x",
                "loot:ore",
                note="环：这条路径上已经出现过，不再展开",
            ),
        ),
    )
    graph = route_graph_ra.to_path_graph([_tree(1, 10.0, root)], "minecraft:x")
    back = ("item::minecraft:x", "rule::craft:block", "needcycle:minecraft:x")
    assert back in graph.edges
    assert back in graph.dashed, "回边不能参与层级计算，否则会把 target 推离最底部"
    # 环上的动作不另建节点：那一步已经在同一页里画过
    assert "rule::loot:ore" not in graph.nodes
    # target 自己没有出边，但必须留着
    assert "item::minecraft:x" in graph.nodes


def test_shared_action_is_one_node() -> None:
    """同一动作出现在两条路线里只画一个节点，边也不重复。"""
    shared = _step("minecraft:pickaxe", "craft:pickaxe#0")
    trees = [
        _tree(1, 10.0, _step("minecraft:x", "mine:ore", children=(shared,))),
        _tree(2, 20.0, _step("minecraft:x", "loot:chest", children=(shared,))),
    ]
    graph = route_graph_ra.to_path_graph(trees, "minecraft:x")
    assert sum(1 for key in graph.nodes if key == "rule::craft:pickaxe#0") == 1
    yield_edges = [
        key for key in graph.edges if key[0] == "rule::craft:pickaxe#0" and key[1] == "item::minecraft:pickaxe"
    ]
    assert len(yield_edges) == 1


def test_node_border_follows_the_cheapest_route() -> None:
    """共享节点取它出现过的最优先路线的颜色：最优路线上的节点是红边框。"""
    shared = _step("minecraft:pickaxe", "craft:pickaxe#0")
    trees = [
        _tree(1, 10.0, _step("minecraft:x", "mine:ore", children=(shared,))),
        _tree(2, 20.0, _step("minecraft:x", "loot:chest", children=(shared,))),
    ]
    graph = route_graph_ra.to_path_graph(trees, "minecraft:x")
    assert graph.nodes["item::minecraft:pickaxe"]["border"] == HIGHLIGHT_COLOR
    assert graph.nodes["rule::craft:pickaxe#0"]["border"] == HIGHLIGHT_COLOR


def test_whole_best_route_is_highlighted() -> None:
    """红色贯穿整条最优路线，而不只是连到 target 的那一条边。"""
    shared = _step("minecraft:pickaxe", "craft:pickaxe#0")
    trees = [
        _tree(1, 10.0, _step("minecraft:x", "mine:ore", children=(shared,))),
        _tree(2, 20.0, _step("minecraft:x", "loot:chest", children=(shared,))),
    ]
    graph = route_graph_ra.to_path_graph(trees, "minecraft:x")
    # 最优路线：pickaxe -> mine:ore -> x，两条边都红
    assert graph.edge_colors[("item::minecraft:pickaxe", "rule::mine:ore", "need:craft:pickaxe#0")] == HIGHLIGHT_COLOR
    assert graph.edge_colors[("rule::mine:ore", "item::minecraft:x", "yield:mine:ore")] == HIGHLIGHT_COLOR
    # 次优路线仍是浓淡
    assert graph.edge_colors[("item::minecraft:pickaxe", "rule::loot:chest", "need:craft:pickaxe#0")] != HIGHLIGHT_COLOR


def test_paid_once_placeholder_is_not_drawn() -> None:
    """「once 前置，链上游已付过」是记账占位，不该变成节点 + 一条「获得」边。"""
    paid = Step(
        item="capability:lava_access",
        action_id="",
        unit_cost=0.0,
        need=1.0,
        calls=0.0,
        subtotal=0.0,
        note="once 前置，链上游已付过",
    )
    root = _step("minecraft:x", "make", cost=5.0, children=(paid,))
    graph = route_graph_ra.to_path_graph([_tree(1, 5.0, root)], "minecraft:x")
    assert not [key for key in graph.nodes if "capability:lava_access@" in key]
    # 前置关系仍保留：item → rule
    assert (
        "item::capability:lava_access",
        "rule::make",
        "need:capability:lava_access",
    ) in graph.edges
    # 没有「获得」边连到那个占位（只有 target 的候选边与前置边）
    assert not [
        key for key in graph.edges if key[0].startswith("rule::capability:lava_access@")
    ]


def test_upstream_candidates_are_expanded() -> None:
    """上游物品有第二个来源时，图上要出现第二条规则（item 节点两条入边）。"""
    actionset = ActionSet(
        "branch",
        (
            Action(
                "make",
                "minecraft:t",
                1.0,
                {"minecraft:t": 1.0},
                requires=(Requirement("minecraft:p", 1.0, MODE_PER_USE),),
            ),
            Action("p1", "minecraft:p", 1.0, {"minecraft:p": 1.0}),
            Action("p2", "minecraft:p", 4.0, {"minecraft:p": 1.0}),
        ),
    )
    solved = solve(actionset, "minecraft:t", 1.0, prune_dominated=False)
    trees = rank_paths(actionset, solved, "minecraft:t", limit=1)
    plain = route_graph_ra.to_path_graph(trees, "minecraft:t")
    assert "rule::p2" not in plain.nodes

    branched = route_graph_ra.to_path_graph(
        trees, "minecraft:t", solved=solved, actionset=actionset, branches=0
    )
    assert "rule::p2" in branched.nodes
    assert ("rule::p2", "item::minecraft:p", "yield:p2") in branched.edges
    # 最优那条仍标红，候选走浓淡
    assert branched.edge_colors[("rule::p1", "item::minecraft:p", "yield:p1")] == HIGHLIGHT_COLOR
    assert branched.edge_colors[("rule::p2", "item::minecraft:p", "yield:p2")] != HIGHLIGHT_COLOR


def test_branch_expansion_respects_the_node_budget() -> None:
    """`max_nodes` 到时停止补候选，并留一个显式的「还有 N 条」节点。"""
    actionset = ActionSet(
        "budget",
        (
            Action(
                "make",
                "minecraft:t",
                1.0,
                {"minecraft:t": 1.0},
                requires=(Requirement("minecraft:p", 1.0, MODE_PER_USE),),
            ),
            Action("p1", "minecraft:p", 1.0, {"minecraft:p": 1.0}),
            *(
                Action(f"p{i}", "minecraft:p", float(i), {"minecraft:p": 1.0})
                for i in range(2, 8)
            ),
        ),
    )
    solved = solve(actionset, "minecraft:t", 1.0, prune_dominated=False)
    trees = rank_paths(actionset, solved, "minecraft:t", limit=1)
    graph = route_graph_ra.to_path_graph(
        trees, "minecraft:t", solved=solved, actionset=actionset, branches=0, max_nodes=4
    )
    assert graph.omitted
    assert any("还有" in info.get("label", "") and "候选" in info.get("label", "")
               for info in graph.nodes.values())


def test_self_serving_step_keeps_its_own_edge() -> None:
    """原料绕回祖先的动作仍要画：回边画成虚线，报价与图上所示来源才对得上。"""
    actionset = ActionSet(
        "cycle",
        (
            Action("p1", "minecraft:p", 1.0, {"minecraft:p": 1.0}),
            Action(
                "p2",
                "minecraft:p",
                1.2,
                {"minecraft:p": 1.0},
                requires=(Requirement("minecraft:q", 1.0, MODE_PER_USE),),
            ),
            Action(
                "q1",
                "minecraft:q",
                1.0,
                {"minecraft:q": 1.0},
                requires=(Requirement("minecraft:p", 1.0, MODE_PER_USE),),
            ),
            Action("q2", "minecraft:q", 40.0, {"minecraft:q": 1.0}),
        ),
    )
    solved = solve(actionset, "minecraft:p", 1.0, prune_dominated=False)
    trees = rank_paths(actionset, solved, "minecraft:p", limit=3)
    assert any(tree.root.action_id == "p2" for tree in trees), "自指候选必须真的出现在候选里"
    graph = route_graph_ra.to_path_graph(
        trees,
        "minecraft:p",
        solved=solved,
        actionset=actionset,
        branches=0,
        ratio_limit=1.5,
    )
    # q1 的 p 前置是绕回 target 的一步：动作照画，回边虚线。
    assert "rule::q1" in graph.nodes
    back = ("item::minecraft:p", "rule::q1", "needcycle:minecraft:p")
    assert back in graph.edges
    assert back in graph.dashed
    # q 有实线来源后，贵的 q2 仍按阈值拦掉，不因「没人画」而误放。
    assert "rule::q2" not in graph.nodes


def test_omitted_node_lists_the_hidden_candidates() -> None:
    """「还有 N 条候选」的 tooltip 要列出具体是哪几条、各贵多少倍。"""
    actionset = ActionSet(
        "budget",
        (
            Action(
                "make",
                "minecraft:t",
                1.0,
                {"minecraft:t": 1.0},
                requires=(Requirement("minecraft:p", 1.0, MODE_PER_USE),),
            ),
            Action("p1", "minecraft:p", 1.0, {"minecraft:p": 1.0}),
            Action("p2", "minecraft:p", 100.0, {"minecraft:p": 1.0}),
        ),
    )
    solved = solve(actionset, "minecraft:t", 1.0, prune_dominated=False)
    trees = rank_paths(actionset, solved, "minecraft:t", limit=1)
    graph = route_graph_ra.to_path_graph(
        trees, "minecraft:t", solved=solved, actionset=actionset,
        branches=0, ratio_limit=1.5,
    )
    omitted = [info for info in graph.nodes.values() if info.get("sort") == "omitted"]
    assert len(omitted) == 1
    tooltip = omitted[0]["tooltip"]
    assert "p2" in tooltip
    assert "100.0×" in tooltip
    # 换行分成多行，不是一长串
    assert tooltip.count("\n") >= 2


def test_self_serving_candidate_is_not_drawn() -> None:
    """依赖回到自己的候选不画：`diamond` 走「钻石块分解」时，钻石块又要钻石合成。

    画出来只剩一个没有入边的合成节点，位置比真正的来源更靠上游，看着像上游的一步。
    """
    actionset = ActionSet(
        "cycle",
        (
            Action(
                "make",
                "minecraft:t",
                1.0,
                {"minecraft:t": 1.0},
                requires=(Requirement("minecraft:p", 1.0, MODE_PER_USE),),
            ),
            Action("p1", "minecraft:p", 1.0, {"minecraft:p": 1.0}),
            Action(
                "p2",
                "minecraft:p",
                5.0,
                {"minecraft:p": 1.0},
                requires=(Requirement("minecraft:t", 1.0, MODE_PER_USE),),
            ),
        ),
    )
    solved = solve(actionset, "minecraft:t", 1.0, prune_dominated=False)
    trees = rank_paths(actionset, solved, "minecraft:t", limit=1)
    graph = route_graph_ra.to_path_graph(
        trees, "minecraft:t", solved=solved, actionset=actionset, branches=0
    )
    assert "rule::p2" not in graph.nodes
    assert "item::minecraft:t" in graph.nodes and "rule::p1" in graph.nodes
    # 不静默丢：留计数节点，tooltip 说清是「依赖自己」而不是「太贵」。
    tooltip = next(
        info["tooltip"] for info in graph.nodes.values() if info.get("sort") == "omitted"
    )
    assert "依赖自己" in tooltip
    assert "贵过阈值" not in tooltip


def test_candidate_expansion_keeps_the_best_route_red() -> None:
    """补候选不能把主路线已在用的来源当成「第二来源」重画、把红线刷淡。

    曲线是按「拿 1 件的全价」排的：`p_cheap` 拿 1 件 2.0，`p_head` 要 6.0；
    但拿 10 件时 `p_head` 只 15.0，`p_cheap` 要 20.0，主路线走的是 `p_head`。
    若补候选仍以曲线第一段为基准，就会把 `p_head` 以 0.75× 重画成蓝色。
    """
    actionset = ActionSet(
        "repaint",
        (
            Action(
                "make",
                "minecraft:t",
                1.0,
                {"minecraft:t": 1.0},
                requires=(Requirement("minecraft:p", 10.0, MODE_PER_USE),),
            ),
            Action(
                "p_head",
                "minecraft:p",
                1.0,
                {"minecraft:p": 1.0},
                overhead=(("setup", 5.0),),
            ),
            Action("p_cheap", "minecraft:p", 2.0, {"minecraft:p": 1.0}),
        ),
    )
    solved = solve(actionset, "minecraft:t", 1.0, prune_dominated=False)
    trees = rank_paths(actionset, solved, "minecraft:t", limit=1)
    assert trees[0].root.children[0].action_id == "p_head"

    graph = route_graph_ra.to_path_graph(
        trees, "minecraft:t", solved=solved, actionset=actionset, branches=0
    )
    assert graph.nodes["rule::p_head"]["border"] == HIGHLIGHT_COLOR
    assert graph.nodes["item::minecraft:p"]["border"] == HIGHLIGHT_COLOR
    edge = ("rule::p_head", "item::minecraft:p", "yield:p_head")
    assert graph.edge_colors[edge] == HIGHLIGHT_COLOR
    # 第二来源照画，按 10 件的代价只贵 1.33 倍，走候选色。
    assert graph.nodes["rule::p_cheap"]["border"] != HIGHLIGHT_COLOR


def test_whole_trip_label_shows_actual_calls() -> None:
    """整次口径的树写实际次数，不写「单价 × 0.54」这种不存在的次数。"""
    root = replace(
        _step("minecraft:t", "loot:trip", cost=17.41, need=3.0),
        subtotal=52.22,
        whole_calls=1.0,
    )
    graph = route_graph_ra.to_path_graph([_tree(1, 52.22, root)], "minecraft:t")
    label = graph.nodes["rule::loot:trip"]["label"]
    assert "总代价 52.22 TU（整次调用）" in label
    tooltip = graph.nodes["rule::loot:trip"]["tooltip"]
    assert "实际执行 1 次 = 52.22 TU" in tooltip


def test_whole_trip_multiplier_only_when_more_than_one() -> None:
    """`×1` 是噪音（五张图 99 个节点里 78 个都是 1），只有多趟才写倍数。

    措辞统一成「整次调用」而不是「整趟」：`kill:` / `roam:` / `cast:` 说「趟」是错词。
    """
    once = replace(
        _step("minecraft:t", "kill:t", cost=42.11), subtotal=42.11, whole_calls=1.0
    )
    many = replace(
        _step("minecraft:u", "roam:u", cost=158.23), subtotal=158.23, whole_calls=12.0
    )
    graph = route_graph_ra.to_path_graph(
        [_tree(1, 42.11, once), _tree(2, 158.23, many)], "minecraft:t"
    )
    assert "（整次调用）" in graph.nodes["rule::kill:t"]["label"]
    assert "×1" not in graph.nodes["rule::kill:t"]["label"]
    assert "（整次调用 ×12）" in graph.nodes["rule::roam:u"]["label"]


def test_whole_trip_breaks_down_into_own_plus_children() -> None:
    """整次的算式要能加回去：`本动作 + Σ前置 = 总数`。

    只剩一个总数就失去了可核对性——读者没法判断 91.85 是哪几笔凑出来的。
    """
    travel = replace(
        _step("capability:sites", "travel:sites", cost=38.58, need=1.0),
        subtotal=38.58,
        own_unit_cost=38.58,
        whole_calls=1.0,
    )
    pick = replace(
        _step("minecraft:pick", "craft:pick", cost=51.15, need=1.0),
        subtotal=51.15,
        own_unit_cost=51.15,
        whole_calls=1.0,
    )
    root = replace(
        _step("minecraft:t", "loot:x", cost=91.85, need=1.0),
        subtotal=91.85,
        own_unit_cost=2.12,
        whole_calls=1.0,
        children=(travel, pick),
    )
    graph = route_graph_ra.to_path_graph([_tree(1, 91.85, root)], "minecraft:t")
    label = graph.nodes["rule::loot:x"]["label"]
    assert "总代价 91.85 TU（整次调用）" in label
    # 数字与名称**分两行**、按列对齐：数值自洽（2.12 + 38.58 + 51.15 = 91.85）
    assert '<TD ALIGN="CENTER">= 2.12</TD>' in label
    assert '<TD ALIGN="CENTER">+ 38.58</TD>' in label
    assert '<TD ALIGN="CENTER">+ 51.15</TD>' in label
    assert '<TD ALIGN="CENTER">本动作</TD>' in label
    assert '<TD ALIGN="CENTER">capability:sites</TD>' in label
    assert '<TD ALIGN="CENTER">pick</TD>' in label
    # 数字行在前、名称行在后（上下对齐而不是串一行）
    assert label.index("2.12") < label.index("本动作")


def test_header_rows_span_every_column() -> None:
    """标题行必须跨满整张表，否则会把第 1 列撑宽、把「=」推到很右边。

    反例：标题只占第 1 列时，`loot_block:deepslate_diamond_ore:mineshafts`
    这种长标题会把该列撑到两百多像素宽，算式第一项离左边框就有大片空白。
    """
    child = replace(
        _step("minecraft:a", "x:a", cost=1.0, need=1.0),
        subtotal=1.0,
        own_unit_cost=1.0,
        whole_calls=1.0,
    )
    root = replace(
        _step("minecraft:t", "loot:x", cost=2.0, need=1.0),
        subtotal=2.0,
        own_unit_cost=1.0,
        whole_calls=1.0,
        children=(child,),
    )
    graph = route_graph_ra.to_path_graph([_tree(1, 2.0, root)], "minecraft:t")
    label = graph.nodes["rule::loot:x"]["label"]
    # 两项 → 2 列；标题、描述、总代价、备注都应是 COLSPAN="2"
    assert '<TD COLSPAN="2" ALIGN="LEFT">loot:x</TD>' in label
    assert '<TD COLSPAN="2" ALIGN="LEFT">总代价 2.00 TU' in label
    # 算式两行都没有 COLSPAN（它们按列落位）
    assert '<TD COLSPAN="2" ALIGN="CENTER">' not in label
    # 等号/加号与数值同格，避免「号」本身占掉一整列
    assert '<TD ALIGN="CENTER">= 1.00</TD>' in label


def test_whole_trip_without_children_has_no_empty_breakdown() -> None:
    """没有前置时只给总数，不硬造一个空的分解行。"""
    solo = replace(
        _step("minecraft:p", "loot:trip", cost=2.12, need=1.0),
        subtotal=2.12,
        own_unit_cost=2.12,
        whole_calls=1.0,
    )
    graph = route_graph_ra.to_path_graph([_tree(1, 2.12, solo)], "minecraft:p")
    label = graph.nodes["rule::loot:trip"]["label"]
    assert "总代价 2.12 TU（整次调用）" in label
    assert "本动作" not in label


def test_paid_once_child_is_listed_as_zero_not_dropped() -> None:
    """整次口径去重后记 0 的共享前置必须留在算式里，并标明「已付」。

    省掉它，读者对不上「这一份哪来的」，也看不出总额为什么没把这一份算进去。
    """
    paid = replace(
        _step(
            "capability:nether_access",
            "",  # 占位节点没有动作可画，`emit` 只登记物品节点
            cost=0.0,
            need=1.0,
            note="链上游已付过，这里不再计费",
        ),
        subtotal=0.0,
        own_unit_cost=0.0,
        whole_calls=0.0,
    )
    root = replace(
        _step("minecraft:t", "portal:frame", cost=157.73, need=1.0),
        subtotal=157.73,
        own_unit_cost=3.20,
        whole_calls=1.0,
        children=(paid,),
    )
    graph = route_graph_ra.to_path_graph([_tree(1, 157.73, root)], "minecraft:t")
    label = graph.nodes["rule::portal:frame"]["label"]
    assert '<TD ALIGN="CENTER">+ 0.00</TD>' in label
    assert "capability:nether_access（已付）" in label
    tooltip = graph.nodes["rule::portal:frame"]["tooltip"]
    assert "= 3.20 + 0.00" in tooltip
    assert "capability:nether_access（已付）" in tooltip
    # 占位本身不画动作节点，前置边只挂一句「为什么是 0」
    assert graph.nodes["item::capability:nether_access"]["label"] == "capability:nether_access"
    assert "rule::capability:nether_access" not in graph.nodes
    edge_tooltip = graph.edges[
        ("item::capability:nether_access", "rule::portal:frame", "need:capability:nether_access")
    ][1]
    assert "记 0 TU" in edge_tooltip


def test_skipped_paths_are_marked_on_the_graph() -> None:
    """`--graph-paths` 截掉的路线必须在图上留痕。

    不标出来，读者会以为「这张图就是全部候选」——`diamond` 实际有 19 条，
    默认只画 3 条。
    """
    best = _step("minecraft:t", "a:best", cost=10.0)
    graph = route_graph_ra.to_path_graph(
        [_tree(1, 10.0, best)],
        "minecraft:t",
        skipped_paths=(
            (2, 2.5, 25.0, "loot:second"),
            (3, 9.0, 90.0, "loot:third"),
        ),
    )
    node = graph.nodes["paths_omitted"]
    assert "还有 2 条路线" in node["label"]
    assert "loot:second" in node["tooltip"] and "2.5×" in node["tooltip"]
    edge = ("paths_omitted", "item::minecraft:t", "paths_omitted")
    assert edge in graph.edges
    assert edge in graph.dashed
    assert graph.edges[edge][0] == "2 条"


def test_skipped_paths_absent_when_nothing_was_dropped() -> None:
    """全都画出来时不留这个节点——空的「还有 0 条」只是噪音。"""
    graph = route_graph_ra.to_path_graph(
        [_tree(1, 10.0, _step("minecraft:t", "a:best", cost=10.0))],
        "minecraft:t",
    )
    assert "paths_omitted" not in graph.nodes


def test_html_notes_skipped_paths_below_the_table() -> None:
    """表格下写明「还有几条、最便宜的是哪条、怎么全画」。"""
    trees = [_tree(1, 10.0, _step("minecraft:t", "a:best", cost=10.0))]
    html = route_graph_ra.render_html(
        "<svg></svg>",
        trees,
        "minecraft:t",
        skipped_paths=((2, 2.5, 25.0, "loot:second"), (3, 9.0, 90.0, "loot:third")),
    )
    assert "只画了前 1 条路线" in html
    assert "还有 <b>2</b> 条没画" in html
    assert "loot:second" in html and "2.50×" in html
    # 一次性给出「要画全该传几」——读者不必自己算 1 + 2
    assert "--graph-paths 3" in html
    # 明细折进 details，不占正文
    assert "<details><summary>未画的 2 条" in html


def test_run_block_matches_ra_layout() -> None:
    """参数照 RA 的版式：在图**上方**、grid 铺开、**不折叠**。

    RA 的理由是「这张表的用途是复算」，折叠就违背了它；位置在图之上是因为
    读者要先知道「用什么参数画的」再看图。
    """
    trees = [_tree(1, 10.0, _step("minecraft:x", "a"))]
    run = route_graph_ra.RunInfo(
        command="bg plan --item minecraft:x --pack-version 26.3 --graph-out PATH",
        entries=(("--item", "minecraft:x"), ("--pack-version", "26.3")),
        action_count=42,
        stats_digest="abcdef1234567890",
    )
    html = route_graph_ra.render_html(
        "<svg></svg>", trees, "minecraft:x", run=run, counts={"图形节点": 7, "边": 9}
    )
    graph_at = html.index('<div class="card">')
    params_at = html.index("生成参数")
    # 参数在图之前
    assert params_at < graph_at
    # grid 铺开的每一项都在（不被折叠吞掉）
    assert '<div class="params">' in html
    assert "--item minecraft:x" in html and "--pack-version 26.3" in html
    assert "bg plan --item minecraft:x" in html
    # 统计摘要与输入指纹
    assert "图形节点 7 个" in html and "边 9 个" in html
    assert "动作图 42 条" in html
    assert "abcdef123456" in html
    assert "abcdef1234567890" not in html


def test_output_path_is_not_part_of_the_parameters() -> None:
    """`--graph-out` 只是写到哪里，与图内容无关，不进参数表。

    读者拿到 HTML 时文件已经在磁盘上了；显示具体路径还会把本机目录带进产物。
    命令里用占位符 `PATH`，不用尖括号（那是 shell 重定向）。
    """
    from bingo_grading.cli.commands.solve import _display_path, _run_info

    class Args:
        item = "minecraft:x"
        count = 1.0
        pack_version = "26.3"
        graph_paths = 3
        graph_depth = 16
        graph_frontiers = 8
        graph_branches = 3
        graph_max_nodes = 500
        graph_branch_ratio = 1.5
        no_prune = False
        dimension = "minecraft:overworld"
        stats = None
        index = None

    class Actions:
        actions = (object(), object())

    run = _run_info(Args(), Actions())
    assert not [name for name, _ in run.entries if name == "--graph-out"]
    assert "--graph-out PATH" in run.command
    assert "<" not in run.command and ">" not in run.command
    # 绝对路径只留文件名（防用户名进产物），相对路径原样保留
    assert _display_path("D:/users/someone/x.jsonl") == "x.jsonl"
    assert _display_path("build/26.3/source_index.jsonl") == "build/26.3/source_index.jsonl"


def test_html_legend_is_short_and_details_are_collapsed() -> None:
    """开头文字要短：读图必需的留在明面，一次性说明收进 <details>。"""
    trees = [_tree(1, 10.0, _step("minecraft:x", "a"))]
    html = route_graph_ra.render_html("<svg></svg>", trees, "minecraft:x")
    head = html.split('<div class="card">', 1)[0]
    # 核心语义必须在明面
    assert "椭圆 = 物品" in head
    assert "红色 = 最优路线" in head
    assert "整次调用" in head
    # 细节放进折叠区
    assert "<details>" in head
    folded = head.split("<details>", 1)[1]
    assert "跨节点" in folded
    assert "回边" in folded


def test_no_run_info_means_no_param_block() -> None:
    """没有生成参数时不画空块（旧的调用方式仍然可用）。"""
    trees = [_tree(1, 10.0, _step("minecraft:x", "a"))]
    html = route_graph_ra.render_html("<svg></svg>", trees, "minecraft:x")
    assert "生成参数" not in html


def test_single_route_is_highlighted() -> None:
    """只有一条路线时整条标红：红色表示「这条路」，与对照表 #1 的色块一致。"""
    graph = route_graph_ra.to_path_graph(
        [_tree(1, 10.0, _step("minecraft:x", "mine:ore"))], "minecraft:x"
    )
    assert graph.edge_colors[("rule::mine:ore", "item::minecraft:x", "yield:mine:ore")] == HIGHLIGHT_COLOR
    assert graph.nodes["rule::mine:ore"]["border"] == HIGHLIGHT_COLOR


def test_same_price_routes_keep_one_red_marker(tmp_path) -> None:
    """同价的两条路线只让第一条带红色锚点，对照表名次并列。"""
    trees = [
        _tree(1, 10.0, _step("minecraft:x", "a")),
        _tree(1, 10.0, _step("minecraft:x", "b")),
    ]
    graph = route_graph_ra.to_path_graph(trees, "minecraft:x")
    assert graph.edge_colors[("rule::a", "item::minecraft:x", "yield:a")] == HIGHLIGHT_COLOR
    assert graph.edge_colors[("rule::b", "item::minecraft:x", "yield:b")] == BEST_COLOR
    html = route_graph_ra.render_html("<svg></svg>", trees, "minecraft:x")
    assert html.count(HIGHLIGHT_COLOR) == 1
    assert html.count("<td>#1</td>") == 2


def test_frontier_table_renders() -> None:
    """前置脆弱点表要写清替代来源与整条路线的差额。"""
    trees = [_tree(1, 10.0, _step("minecraft:x", "a"))]
    html = route_graph_ra.render_html(
        "<svg></svg>",
        trees,
        "minecraft:x",
        frontiers=[
            {
                "item": "minecraft:p",
                "action": "p1",
                "alternative": "p2",
                "route_total": 15.0,
                "delta": 3.0,
                "delta_ratio": 0.3,
            }
        ],
    )
    assert "前置替换成本" in html
    assert "p1" in html and "p2" in html
    assert "+3.00（+30.0%）" in html


def test_route_table_shows_three_decimals() -> None:
    """近价路线在两位小数下都显示 1.00×，对照表要给到千分位。"""
    trees = [
        _tree(1, 10.0, _step("minecraft:x", "a")),
        _tree(2, 10.028, _step("minecraft:x", "b")),
    ]
    html = route_graph_ra.render_html("<svg></svg>", trees, "minecraft:x")
    assert "1.000×" in html
    assert "1.003×" in html
    assert "1.00×" not in html


def test_root_edge_keeps_its_own_total() -> None:
    """同一动作在多棵树里作根时，边上的总成本不能串成别人的。"""
    trees = [
        _tree(1, 10.0, _step("minecraft:x", "a")),
        _tree(2, 12.0, _step("minecraft:x", "a", note="同动作的第二条路线")),
    ]
    graph = route_graph_ra.to_path_graph(trees, "minecraft:x")
    edge = ("rule::a", "item::minecraft:x", "yield:a")
    assert graph.edges[edge][0] == "10.0 TU"


def test_write_outputs_writes_dot_and_html(tmp_path) -> None:
    trees = [_tree(1, 10.0, _step("minecraft:x", "mine:ore"))]
    written = route_graph_ra.write_outputs(tmp_path / "graph", trees, "minecraft:x")
    names = {path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for path in written}
    assert "graph.dot" in names
    assert "graph.html" in names
    dot = (tmp_path / "graph.dot").read_text(encoding="utf-8")
    assert dot.startswith("digraph") and "label=<<" in dot
    html = (tmp_path / "graph.html").read_text(encoding="utf-8")
    assert "路线对照" in html
    if route_graph_ra.graphviz_available():
        assert "graph.svg" in names
        assert "<svg" in html
    else:
        assert "Graphviz" in html


def test_empty_trees_write_nothing(tmp_path) -> None:
    assert route_graph_ra.write_outputs(tmp_path / "graph", [], "minecraft:x") == []


@pytest.mark.skipif(not route_graph_ra.graphviz_available(), reason="没有 Graphviz（dot）")
def test_dot_is_accepted_by_graphviz() -> None:
    trees = [_tree(1, 10.0, _step("minecraft:x", "mine:ore", note="递归深度用尽"))]
    graph = route_graph_ra.to_path_graph(trees, "minecraft:x")
    svg = route_graph_ra.dot_to_svg(route_graph_ra.build_dot(graph, route_graph_ra.PathOptions()))
    assert svg is not None and "<svg" in svg
