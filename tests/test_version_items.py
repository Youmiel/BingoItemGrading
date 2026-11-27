"""Items that only exist in some versions must not become prerequisites elsewhere."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bingo_grading import paths
from bingo_grading.case.context import CaseContext
from bingo_grading.case.context.model import _registry_entries
from bingo_grading.sources import actions
from bingo_grading.stats.model import WorldStatStore

ROOT = Path(__file__).resolve().parents[1]


def _registry(tmp_path: Path, items: list[str]) -> Path:
    path = tmp_path / "registry.jsonl"
    rows = [
        {"kind": "registry", "payload": {"registry": "minecraft:block", "entries": ["minecraft:stone"]}},
        {"kind": "registry", "payload": {"registry": "minecraft:item", "entries": items}},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def test_item_registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _registry(tmp_path, ["minecraft:map", "minecraft:filled_map"])
    monkeypatch.setattr(paths, "registry_facts", lambda pack: paths.Resolved(path, paths.LOCAL))
    _registry_entries.cache_clear()
    context = CaseContext(store=WorldStatStore())
    assert context.item_registered("minecraft:map", pack="test") is True
    assert context.item_registered("minecraft:buried_treasure_map", pack="test") is False
    # Block registry entries are not items.
    assert context.item_registered("minecraft:stone", pack="test") is False


def test_missing_registry_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        paths, "registry_facts", lambda pack: paths.Resolved(tmp_path / "absent", paths.MISSING)
    )
    assert CaseContext(store=WorldStatStore()).item_registered("minecraft:map", pack="test") is None


LEGACY = ROOT / "data" / "stats" / "1.18.2"


@pytest.mark.skipif(not LEGACY.exists(), reason=f"missing snapshot {LEGACY}")
def test_legacy_version_routes_do_not_require_new_items() -> None:
    ctx = CaseContext.load(LEGACY)
    built = actions.build(ctx, "minecraft:heart_of_the_sea")
    required = {r.item for a in built.actionset().actions for r in a.requires}
    assert "minecraft:buried_treasure_map" not in required
    assert "minecraft:heart_of_the_sea" not in built.missing_prereqs

    # Raids need an outpost trip when the sample has outposts; otherwise they are absent.
    raid = actions.build(ctx, "minecraft:totem_of_undying")
    for action in raid.actionset().actions:
        if not action.action_id.startswith("raid:"):
            continue
        items = {r.item for r in action.requires}
        assert "minecraft:ominous_bottle" not in items
        assert "capability:minecraft:pillager_outposts" in items
    assert "minecraft:ominous_bottle" not in raid.missing_prereqs
