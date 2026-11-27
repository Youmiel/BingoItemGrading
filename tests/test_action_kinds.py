"""动作前缀的计数口径：每个前缀都要有明确属性与理由。

`ACTION_KINDS` 是唯一事实来源，`ACTION_PREFIXES` / `ATOMIC_PREFIXES` 都由它派生。
新增前缀必须同时决定「离散还是可分割」并写理由——否则求解器会静默用错口径
（`kill:` 就漏过一次：杀 0.75 只铁傀儡）。
"""

from __future__ import annotations

from bingo_grading.plan.actions import (
    ACTION_KINDS,
    ACTION_PREFIXES,
    ATOMIC_PREFIXES,
    is_discrete,
    kind_of,
)


def test_every_prefix_is_classified() -> None:
    assert set(ACTION_PREFIXES) == {kind.prefix for kind in ACTION_KINDS}
    assert len(ACTION_KINDS) == len(ACTION_PREFIXES)


def test_atomic_prefixes_are_derived_from_kinds() -> None:
    assert ATOMIC_PREFIXES == tuple(
        f"{kind.prefix}:" for kind in ACTION_KINDS if kind.discrete
    )


def test_every_kind_has_a_reason() -> None:
    for kind in ACTION_KINDS:
        assert kind.why.strip(), f"{kind.prefix} 缺理由"
        assert len(kind.why) >= 6, f"{kind.prefix} 的理由太短：{kind.why!r}"


def test_prefixes_are_bare_identifiers() -> None:
    for kind in ACTION_KINDS:
        assert ":" not in kind.prefix, kind.prefix
        assert kind.prefix == kind.prefix.strip().lower()


def test_kind_of_matches_the_whole_prefix() -> None:
    """按冒号前的整段查表：`loot_block:` 不能被当成 `loot:`。"""
    assert kind_of("loot:shipwrecks").prefix == "loot"
    assert kind_of("loot_block:snow:ruined_portals").prefix == "loot_block"
    assert kind_of("bare_name") is None
    assert kind_of("unknown:thing") is None


def test_is_discrete_reads_the_registry() -> None:
    assert is_discrete("loot:villages")
    assert is_discrete("trade:armorer/1:iron_boots:armorer/1/emerald_iron_boots")
    assert not is_discrete("craft:stick#0")
    assert not is_discrete("bare_name")


def test_divisible_kinds_are_the_linear_ones() -> None:
    """可分割 = 一次调用就是「处理一件」的线性动作。

    锁定这份清单，是为了让「新增一个前缀」变成一次显式决定：要改这里，
    就得说明为什么它能按件摊，而不是默认沿用某个旧判断。

    `wear:` 不在此列：一次调用是「磨到掉一级」的完整事件，12% 概率已折进成本，
    产出确定 1 件（早年误归可分割，理由写成「按使用次数线性」）。
    """
    assert {kind.prefix for kind in ACTION_KINDS if not kind.discrete} == {
        "mine",
        "craft",
        "smelt",
        "transmute",
        "convert",
        "oxidize",
        "tool",
    }


def test_wear_is_discrete_not_linear() -> None:
    """铁砧磨损是概率事件，但产出是确定的 1 件——不能按「使用次数」线性摊。"""
    kind = kind_of("wear:chipped_anvil")
    assert kind is not None and kind.discrete
    assert "概率" in kind.why
