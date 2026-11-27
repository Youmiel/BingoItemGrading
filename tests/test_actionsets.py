"""落盘动作图：序列化往返、指纹失效、目标子图。"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from bingo_grading.plan import Action, ActionSet, Requirement
from bingo_grading.plan import solve as solve_plan
from bingo_grading.plan.actions import MODE_ONCE, MODE_PER_USE
from bingo_grading.sources import actionsets
from bingo_grading.sources.actions.graph import GraphActions
from bingo_grading.case.sources.bundle import ActionBundle


def _actionset() -> ActionSet:
    return ActionSet(
        label="probe",
        actions=(
            Action(
                action_id="craft:b",
                item="minecraft:b",
                cost=1.0,
                yields={"minecraft:b": 1.0},
            ),
            Action(
                action_id="craft:a",
                item="minecraft:a",
                cost=1 / 3,
                yields={"minecraft:a": 2.0, "minecraft:c": 0.5},
                capacity=3.0,
                requires=(
                    Requirement("minecraft:b", 2.0),
                    Requirement("minecraft:fuel", 1.0, MODE_PER_USE),
                ),
                label="两个 b 换一个 a",
                source="calibration",
                race="trips",
                equivalents={"minecraft:c": 0.25},
                overhead=(("reach:overworld:y16", 2.5), ("reach:overworld:y16", 0.5)),
            ),
            Action(
                action_id="mine:fuel",
                item="minecraft:fuel",
                cost=0.0,
                yields={"minecraft:fuel": 4.0},
            ),
            Action(
                action_id="loot:unrelated",
                item="minecraft:z",
                cost=9.0,
                yields={"minecraft:z": 1.0},
            ),
        ),
    )


def test_actionset_roundtrip_is_bit_exact(tmp_path: Path) -> None:
    """落盘的图必须逐位往返：成本舍入会让 whole_cost 漂移。"""
    original = _actionset()
    path = tmp_path / "actions.jsonl"
    header = actionsets.write(
        path,
        original,
        pack="26.3",
        budget=13,
        sources={"minecraft:a": 1, "minecraft:b": 1},
        uncovered={"缺方块密度:x": 2},
        stats_digest="abc",
    )
    assert header["actions"] == len(original.actions)
    read_back = actionsets.read(path, expect_digest="abc")
    assert read_back.budget == 13
    assert read_back.sources == {"minecraft:a": 1, "minecraft:b": 1}
    assert read_back.uncovered == {"缺方块密度:x": 2}
    assert read_back.targets == ["minecraft:a", "minecraft:b"]
    got = {action.action_id: action for action in read_back.actionset.actions}
    want = {action.action_id: action for action in original.actions}
    assert got == want
    # 完整浮点：`1 / 3` 不能被舍入成 0.3333。
    assert got["craft:a"].cost == 1 / 3
    # `overhead` 同名多条必须都保留（不同纵段的竖井各收一次）。
    assert got["craft:a"].overhead == (
        ("reach:overworld:y16", 2.5),
        ("reach:overworld:y16", 0.5),
    )


def test_write_is_byte_stable(tmp_path: Path) -> None:
    """同一张图写两次逐字节一致：动作按 `action_id` 排序。"""
    first = tmp_path / "one.jsonl"
    second = tmp_path / "two.jsonl"
    for path in (first, second):
        actionsets.write(
            path,
            _actionset(),
            pack="26.3",
            budget=13,
            sources={"minecraft:a": 1},
            uncovered={},
            stats_digest="abc",
        )
    assert first.read_bytes() == second.read_bytes()


def test_read_rejects_stale_digest(tmp_path: Path) -> None:
    """输入指纹变了必须报错，不静默使用旧图。"""
    path = tmp_path / "actions.jsonl"
    actionsets.write(
        path,
        _actionset(),
        pack="26.3",
        budget=13,
        sources={},
        uncovered={},
        stats_digest="old",
    )
    with pytest.raises(actionsets.ActionsFileError) as excinfo:
        actionsets.read(path, expect_digest="new")
    assert "index actions" in str(excinfo.value)


def test_read_rejects_unknown_schema(tmp_path: Path) -> None:
    path = tmp_path / "actions.jsonl"
    header = {
        "actions_schema": 999,
        "label": "probe",
        "pack_version": "26.3",
        "stats_digest": "abc",
        "actions": 0,
        "targets": 0,
        "budget": 1,
        "sources": {},
        "uncovered": {},
    }
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")
    with pytest.raises(actionsets.ActionsFileError) as excinfo:
        actionsets.read(path)
    assert "actions_schema" in str(excinfo.value)


def test_missing_file_is_distinguishable(tmp_path: Path) -> None:
    """`--all` 首次自动构建只认「不存在」，schema 不符与过期都要求人工重建。"""
    with pytest.raises(actionsets.ActionsFileMissing):
        actionsets.read(tmp_path / "nope.jsonl")


def test_digest_tracks_input_content(tmp_path: Path) -> None:
    index = tmp_path / "source_index.jsonl"
    stats = tmp_path / "world_stat.jsonl"
    index.write_text('{"index_schema": 3}\n', encoding="utf-8")
    stats.write_text('{"row": 1}\n', encoding="utf-8")
    kwargs = {"pack": "26.3", "index_path": index, "stats_path": stats}
    first = actionsets.digest_inputs(**kwargs)
    assert first == actionsets.digest_inputs(**kwargs)
    stats.write_text('{"row": 2}\n', encoding="utf-8")
    assert actionsets.digest_inputs(**kwargs) != first
    # 文件消失同样算变化：数据补齐之后必须重建。
    changed = actionsets.digest_inputs(**kwargs)
    stats.unlink()
    assert actionsets.digest_inputs(**kwargs) != changed


def test_digest_tracks_build_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同一批输入配上不同的构建逻辑，产出的图不是同一张。"""
    index = tmp_path / "source_index.jsonl"
    stats = tmp_path / "world_stat.jsonl"
    index.write_text('{"index_schema": 3}\n', encoding="utf-8")
    stats.write_text('{"row": 1}\n', encoding="utf-8")
    kwargs = {"pack": "26.3", "index_path": index, "stats_path": stats}
    before = actionsets.digest_inputs(**kwargs)
    monkeypatch.setattr(actionsets, "BUILD_POLICY", actionsets.BUILD_POLICY + 1)
    assert actionsets.digest_inputs(**kwargs) != before


def test_header_records_no_action_items(tmp_path: Path) -> None:
    """有来源记录、但图上没有动作的物品进头部，写读往返保留。"""
    path = tmp_path / "a.jsonl"
    header = actionsets.write(
        path,
        _actionset(),
        pack="26.3",
        budget=1,
        sources={"minecraft:a": 1},
        uncovered={},
        stats_digest="x",
        no_action_items=["minecraft:missing"],
    )
    assert header["no_action_items"] == 1
    assert header["no_action_examples"] == ["minecraft:missing"]
    assert actionsets.read(path).no_action_examples == ["minecraft:missing"]


def test_header_records_missing_prerequisites(tmp_path: Path) -> None:
    """前置指向不存在的物品要进头部：分层会把这类动作静默丢成 `no_rule`。

    曾经的 bug：工具名拼成 `wood_pickaxe`（正确是 `wooden_pickaxe`），
    所有石头类的挖矿动作被静默丢弃，tuff 从 6 秒变成 602 秒。
    """
    path = tmp_path / "a.jsonl"
    header = actionsets.write(
        path,
        _actionset(),
        pack="26.3",
        budget=1,
        sources={"minecraft:a": 1},
        uncovered={},
        stats_digest="x",
        missing_prereqs={"minecraft:wood_pickaxe": 3},
    )
    assert header["missing_prereq_items"] == 1
    assert header["missing_prereqs"] == {"minecraft:wood_pickaxe": 3}
    assert actionsets.read(path).missing_prereqs == {"minecraft:wood_pickaxe": 3}


def test_settle_prereqs_ignores_capabilities() -> None:
    """`capability:` 不是物品，缺它不算「前置不存在」——由 `uncovered` 单独记账。"""
    result = GraphActions(
        label="t",
        bundle=ActionBundle(label="t"),
    )
    result.emit(
        Action(
            action_id="craft:thing",
            item="minecraft:thing",
            cost=1.0,
            yields={"minecraft:thing": 1.0},
            requires=(
                Requirement("capability:missing_state", 1.0, MODE_ONCE),
                Requirement("minecraft:no_such_tool", 1.0, MODE_ONCE),
            ),
        )
    )
    assert dict(result.settle_prereqs()) == {"minecraft:no_such_tool": 1}


def test_subgraph_keeps_target_chain_only() -> None:
    subset = subgraph_of(_actionset(), "minecraft:a")
    assert {action.action_id for action in subset.actions} == {
        "craft:a",
        "craft:b",
        "mine:fuel",
    }


def test_subgraph_matches_full_solution() -> None:
    """裁掉无关动作不改变目标曲线：拓扑 DP 只依赖目标自己的依赖链。"""
    actionset = _actionset()
    full = solve_plan(actionset, "minecraft:a", 1.0, prune_dominated=False)
    subset = subgraph_of(actionset, "minecraft:a")
    small = solve_plan(subset, "minecraft:a", 1.0, prune_dominated=False)
    want = full.curve_of("minecraft:a").cost(1.0)
    assert small.curve_of("minecraft:a").cost(1.0) == want


def test_subgraph_of_missing_item_is_empty() -> None:
    assert not actionsets.subgraph(_actionset(), "minecraft:absent").actions


def subgraph_of(actionset: ActionSet, item: str) -> ActionSet:
    return actionsets.subgraph(actionset, item)


@pytest.mark.slow
@pytest.mark.skipif(
    not actionsets.default_path("26.3").exists(),
    reason="需要先跑 `bg index actions` 生成落盘动作图",
)
def test_persisted_graph_solves_like_whole_graph() -> None:
    """真实落盘图上，子图求解与全图求解给出同一整次成本。"""
    from bingo_grading import grade as grade_module

    graph = actionsets.read(actionsets.default_path("26.3"))
    assert any(
        math.isinf(action.capacity) for action in graph.actionset.actions
    ), "`capacity: null` 必须读回无限次"
    solved = solve_plan(
        graph.actionset,
        graph.targets[0],
        1.0,
        prune_dominated=False,
    )
    by_id = {action.action_id: action for action in graph.actionset.actions}
    for name in ("minecraft:diamond", "minecraft:coal", "minecraft:glowstone_dust"):
        full, _, _, _ = grade_module.best_whole_cost(
            graph.actionset, solved.curves, name, 1.0, actions_by_id=by_id
        )
        subset = graph.subgraph(name)
        small = solve_plan(subset, name, 1.0, prune_dominated=False)
        whole, _, _, _ = grade_module.best_whole_cost(
            subset, small.curves, name, 1.0
        )
        assert whole == full, name
