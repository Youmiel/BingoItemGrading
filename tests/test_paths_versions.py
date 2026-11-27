"""Path resolution with several versions side by side, including legacy plural directories."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bingo_grading import paths
from bingo_grading.loot.tables import _namespace_roots
from bingo_grading.sources.knowledge import StructureMaps


@pytest.fixture()
def fake_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    ref = tmp_path / "ref"
    monkeypatch.setattr(paths, "DATA_ROOT", data)
    monkeypatch.setattr(paths, "UPSTREAM_ROOT", data / "upstream")
    monkeypatch.setattr(paths, "REF_RECIPE_ANALYSIS", ref / "RecipeAnalysis")
    monkeypatch.setattr(paths, "REF_COLLECTOR", ref / "WorldGenStatsCollector")
    return tmp_path


def _run(stats: Path, name: str, version: str | None) -> Path:
    run = stats / name
    run.mkdir(parents=True)
    target = {"minecraftVersion": version} if version else {}
    (run / "summary.json").write_text(json.dumps({"target": target}), encoding="utf-8")
    return run


def test_collector_run_is_picked_per_version(fake_layout: Path) -> None:
    stats = paths.REF_COLLECTOR / "stats"
    older = _run(stats, "2026-11-26T03-00-00-000Z", "26.3")
    _run(stats, "2026-11-26T05-00-00-000Z", "1.18.2")

    assert paths.upstream_collector_run("26.3").path == older
    assert paths.upstream_collector_run("1.18.2").path.name == "2026-11-26T05-00-00-000Z"
    assert paths.upstream_collector_run("1.20.1").layer == paths.MISSING


def test_unversioned_runs_are_still_accepted(fake_layout: Path) -> None:
    run = _run(paths.REF_COLLECTOR / "stats", "2026-11-13T12-00-00-000Z", None)
    assert paths.upstream_collector_run("26.3").path == run


def _cache(name: str, version: str, templates: str) -> Path:
    server = paths.REF_RECIPE_ANALYSIS / ".cache" / name
    (server / "bundler" / "META-INF" / "versions" / version).mkdir(parents=True)
    (server / "structures" / "data" / "minecraft" / templates).mkdir(parents=True)
    return server


def test_structure_cache_is_picked_per_version(fake_layout: Path) -> None:
    new = _cache("server-a14df142f923", "26.3", "structure")
    # Sorts first but belongs to another version and uses the legacy directory.
    old = _cache("server-a64d7dd5273a", "1.18.2", "structures")

    assert paths.upstream_vanilla_structures("26.3").path == new / "structures" / "data"
    assert paths.upstream_vanilla_structures("1.18.2").path == old / "structures" / "data"
    assert paths.upstream_vanilla_structures("1.20.1").layer == paths.MISSING


@pytest.mark.parametrize(
    ("directory", "resolver"),
    [("loot_tables", paths.loot_tables), ("structures", paths.structure_templates)],
)
def test_legacy_plural_data_directories(fake_layout: Path, directory: str, resolver) -> None:
    legacy = paths.UPSTREAM_ROOT / "1.18.2" / "datapack" / "data" / "minecraft" / directory
    legacy.mkdir(parents=True)
    resolved = resolver("1.18.2")
    assert resolved.path == legacy
    assert resolved.layer != paths.MISSING


def test_loot_tables_found_under_legacy_directory(tmp_path: Path) -> None:
    (tmp_path / "data" / "minecraft" / "loot_tables").mkdir(parents=True)
    roots = _namespace_roots(tmp_path / "data")
    assert roots == [("minecraft", tmp_path / "data" / "minecraft" / "loot_tables")]


def test_template_prefix_accepts_legacy_directory() -> None:
    maps = StructureMaps(template_prefixes=(("village/plains/", "minecraft:villages"),))
    assert maps.set_for_template("data/minecraft/structures/village/plains/x.snbt") == "minecraft:villages"
    # A template's own `structures/` subdirectory is not the root.
    nested = StructureMaps(template_prefixes=(("ancient_city/structures/", "minecraft:ancient_cities"),))
    assert (
        nested.set_for_template("data/minecraft/structure/ancient_city/structures/barracks.snbt")
        == "minecraft:ancient_cities"
    )
