"""结构与到达：最近距离、行程、跑图开箱、挖矿道碰运气。"""

from __future__ import annotations

import math

from bingo_grading import gamedata, mining_facts, paths, units
from bingo_grading.mining import CHUNK_FOOTPRINT
from .model import Sourced, WATER_BLOCKS, _code_container_rows, _structure_maps

class StructuresMixin:
    def village_chests_per_site(self, *, dimension: str | None = None) -> Sourced | None:
        """每处村庄能开几个箱子（**实测**，不是估值）。

        取各村庄变体的 `structure_content.containers_per_start` 等权平均。
        只统计有正值可信值的变体：字段缺失或算成 0 的变体记进缺口，
        不按 0 参与平均，也不用常量顶上。
        """
        dimension = dimension or self.dimension
        values: dict[str, float] = {}
        missing: set[str] = set()
        for row in self.store.rows.values():
            if row.stat_type != "structure_content":
                continue
            if not row.subject.startswith("minecraft:village_"):
                continue
            if row.dimension not in ("", dimension):
                continue
            per_start = row.value.get("containers_per_start")
            if isinstance(per_start, (int, float)) and per_start > 0:
                values[row.subject] = max(values.get(row.subject, 0.0), float(per_start))
            else:
                missing.add(row.subject)
        if not values:
            return None
        average = sum(values.values()) / len(values)
        detail = (
            f"{len(values)} 个村庄变体的 containers_per_start 等权平均："
            + "、".join(
                f"{name.split(':')[-1]}={value:.2f}" for name, value in sorted(values.items())
            )
        )
        gaps = sorted(missing - set(values))
        if gaps:
            detail += "；无可信值：" + "、".join(name.split(":")[-1] for name in gaps)
        return Sourced(average, "structure_content:containers_per_start", detail)


    def containers_per_site(
        self,
        structure_set: str,
        *,
        dimension: str | None = None,
    ) -> Sourced | None:
        """某结构集合「每处能开几个箱」——取它的各个变体等权平均。

        密度是按集合测的（`minecraft:villages`），箱数是按变体测的
        （`minecraft:village_plains` 每处 2.30 个），两边靠
        `knowledge/structure_sets.toml` 的 `[structure_members]` 对齐。
        没有正值的变体不按 0 参与平均，也不用常量顶上。
        """
        dimension = dimension or self.dimension
        members = _structure_maps().members_of(structure_set)
        if not members:
            return None
        values: dict[str, float] = {}
        candidates = [
            row
            for member in members
            for row in self.store.of("structure_content", member)
            if row.dimension in ("", dimension)
        ]
        for row in candidates:
            per_start = row.value.get("containers_per_start")
            if not (isinstance(per_start, (int, float)) and per_start > 0):
                # 模板分析产出的行只给 `containers`（同一口径、字段名不同），
                # 两批都要收，否则 40 个变体会被当成「缺箱数」。
                per_start = row.value.get("containers")
            if isinstance(per_start, (int, float)) and per_start > 0:
                values[row.subject] = max(values.get(row.subject, 0.0), float(per_start))
        if not values:
            return None
        average = sum(values.values()) / len(values)
        detail = (
            f"{len(values)}/{len(members)} 个变体的每处容器数等权平均："
            + "、".join(
                f"{name.split(':')[-1]}={value:.2f}"
                for name, value in sorted(values.items())
            )
        )
        return Sourced(average, "structure_content:containers_per_start", detail)


    def loot_tables_per_site(
        self,
        structure_set: str,
        table: str,
        *,
        dimension: str | None = None,
    ) -> Sourced | None:
        """每处结构里有几个**这张表**的箱子。

        与 `containers_per_site()` 的区别：那里是容器**总数**（堡垒遗迹 7.2），
        这里是分表的（`bastion_treasure` 只占 **0.1**）。拿总数乘一张表的每箱
        期望，会把「跑一趟能拿到多少」放大几十倍——下界合金锭的单价因此被
        低估了一到两个量级。

        数据来自 `structure_content.loot_tables`（采集侧按结构实测的分表箱数）。
        """
        dimension = dimension or self.dimension
        members = _structure_maps().members_of(structure_set)
        if not members:
            return None
        values: dict[str, float] = {}
        candidates = [
            row
            for member in members
            for row in self.store.of("structure_content", member)
            if row.dimension in ("", dimension)
        ]
        for row in candidates:
            # 两种口径，分母不同，不能混用：
            #   `loot_tables_per_start` = 每**实例**（起点）这张表几个箱 ← 要这个
            #   `loot_tables`           = 每**模板**这张表几个箱（模板行的原有字段）
            # 模板行的 `loot_tables` 分母是模板总数，一个实例由多个模板拼成，
            # 直接拿来当「一趟开几个」会把堡垒遗迹的宝藏箱从 5.7 压到 0.17。
            tables_per_start = row.value.get("loot_tables_per_start") or {}
            per_start = tables_per_start.get(table)
            if not (isinstance(per_start, (int, float)) and per_start > 0):
                per_start = (row.value.get("loot_tables") or {}).get(table)
            if isinstance(per_start, (int, float)) and per_start > 0:
                values[row.subject] = max(values.get(row.subject, 0.0), float(per_start))
        if not values:
            return None
        average = sum(values.values()) / len(values)
        detail = (
            f"{len(values)}/{len(members)} 个变体中 {table.split('/')[-1]} "
            f"每处出现次数等权平均："
            + "、".join(
                f"{name.split(':')[-1]}={value:g}"
                for name, value in sorted(values.items())
            )
        )
        return Sourced(average, "structure_content:loot_tables", detail)


    def code_placed_tables_per_site(
        self,
        structure_set: str,
        table: str,
        *,
        pack: str | None = None,
    ) -> Sourced | None:
        """Chests of this table placed by structure code (upstream `*Pieces.java`).

        `sites` counts per structure piece, so it is only used when the structure has
        neither measured nor template content.
        """
        members = set(_structure_maps().members_of(structure_set))
        if not members:
            return None
        if any(self.store.of("structure_content", member) for member in members):
            return None
        resolved = paths.recipe_export(pack or self.pack_version())
        if not resolved.exists:
            return None
        for row in _code_container_rows(str(resolved.path / "structure_containers.jsonl")):
            if row.get("table") != table:
                continue
            if not members & set(row.get("structures") or ()):
                continue
            sites = row.get("sites")
            if isinstance(sites, (int, float)) and sites > 0:
                sources = "、".join(str(src).rsplit("/", 1)[-1] for src in row.get("sources") or ())
                detail = (
                    f"{structure_set} has no measured or template chest count; using "
                    f"{sites:g} {table.split('/')[-1]} placed by structure code ({sources})"
                )
                return Sourced(float(sites), "recipe_export:structure_containers", detail)
        return None


    def mob_search_cost(
        self,
        subject: str,
        *,
        dimension: str | None = None,
    ) -> tuple[Sourced, Sourced] | None:
        """碰到一只某生物要走的距离与代价。返回 `(代价, 密度出处)`。

        密度是 `chunks_per`（平均几个区块遇见一只）。搜完整片区域要走的
        距离按「边长为 `√chunks_per` 个区块」，期望走一半（走到中心）算。
        高度取实测 `avg_y`：负高度按地下跑图系数。
        """
        dimension = dimension or self.dimension
        for row in self.store.rows.values():
            if row.stat_type != "mob_density":
                continue
            if row.subject != subject or row.dimension != dimension:
                continue
            chunks_per = row.value.get("chunks_per")
            if not isinstance(chunks_per, (int, float)) or chunks_per <= 0:
                continue
            avg_y = row.value.get("avg_y")
            underground = isinstance(avg_y, (int, float)) and avg_y < 0
            # `CHUNK_FOOTPRINT` 是区块**面积**（256 格²），边长要开方。
            side_blocks = math.sqrt(float(chunks_per)) * math.sqrt(CHUNK_FOOTPRINT)
            distance = side_blocks * 0.5
            travel = self.travel(distance, underground=underground)
            return (
                travel,
                Sourced(
                    float(chunks_per),
                    "mob_density",
                    f"{subject} 平均 {chunks_per:.1f} 区块一只 → 搜索半径 "
                    f"{distance:.0f} 格",
                ),
            )
        return None


    def village_villager_count(self, *, dimension: str | None = None) -> Sourced | None:
        """每处村庄有多少个村民。

        用来算「能造多少个牧师」：摆酿造台可以让无业村民转职成牧师，
        一个酿造台还能反复挪位置转多个，所以村民数就是牧师的实际上限。

        算法：全图村民数 ÷ 村庄数。村庄数由 `structure_density` 的 spacing 推出。
        村民数是自然生成时就有的（村庄自带的居民），不含玩家繁殖。
        """
        dimension = dimension or self.dimension
        villagers = None
        for row in self.store.rows.values():
            if (
                row.stat_type == "mob_density"
                and row.subject == "minecraft:villager"
                and row.dimension == dimension
            ):
                villagers = row
                break
        if villagers is None:
            return None

        structure_row = None
        for row in self.store.rows.values():
            if (
                row.stat_type == "structure_density"
                and row.subject == "minecraft:villages"
                and row.dimension == dimension
            ):
                structure_row = row
                break
        if structure_row is None:
            return None

        spacing = structure_row.value.get("spacing")
        if not spacing:
            return None

        chunks_scanned = self._scan_chunks(dimension)
        if not chunks_scanned:
            return None

        per_chunk = villagers.value.get("per_chunk")
        if per_chunk is None:
            return None

        structures = chunks_scanned / (spacing * spacing)
        if structures <= 0:
            return None
        total = per_chunk * chunks_scanned
        return Sourced(
            total / structures,
            "mob_density÷structure_density",
            f"村民 {per_chunk:.4f}/区块 × {chunks_scanned} 区块 = {total:.0f} 个，"
            f"÷ {structures:.1f} 处村庄",
        )


    def _scan_chunks(self, dimension: str | None = None) -> int | None:
        """扫描区块数。只认维度级的 `block_density` 行——它们的分母就是扫描范围。

        群系级的行（`ore_density`）分母是该群系的等效区块数，不能当扫描范围用。
        """
        dimension = dimension or self.dimension
        for row in self.store.rows.values():
            if row.stat_type != "block_density":
                continue
            if row.dimension != dimension or row.biome:
                continue
            chunks = row.value.get("chunks")
            if isinstance(chunks, int) and chunks > 0:
                return chunks
        return None


    def structure_nearest_distance(
        self, subject: str, *, dimension: str | None = None
    ) -> Sourced | None:
        """由采样起点数推期望最近距离（给没有 `structure_density` 的结构用）。

        地牢（monster room）是 feature，不是结构集，统计里没有
        `expected_nearest_distance`；但 `structure_content[measured]` 记了
        扫到多少处（`starts`），配合同一次扫描的区块数就能算密度：

            λ = starts / (区块数 × 256)   处 / 格²
            期望最近距离 = 0.5 / √λ

        这是「随机点到最近一个事件」的泊松期望，**假设你知道它在哪**。
        真正的「搜索」代价高于它，见各案例里的说明。
        """
        dimension = dimension or self.dimension
        content = self.structure_content(subject, source="measured", dimension=dimension)
        if content is None:
            return None
        starts = content[0].get("starts")
        # 分母必须用**同一次采集**的区块数。以前这里借了 `block_density` 行的
        # `chunks`，那是另一次扫描的范围（结构采集扫的区块数不同、且地牢是
        # 三维分布），算出来的最近距离能差一倍以上。
        chunks = content[0].get("chunks")
        source = "structure_content÷scan"
        if not isinstance(chunks, int) or not chunks:
            chunks = self._scan_chunks(dimension)
            source = "structure_content÷block_density(分母借的，可疑)"
        if not starts or not chunks:
            return None
        density = float(starts) / (float(chunks) * float(CHUNK_FOOTPRINT))
        if density <= 0:
            return None
        return Sourced(
            0.5 / math.sqrt(density),
            source,
            f"{subject} 采样 {starts} 处 / {chunks} 区块",
        )


    def nearest_distance(
        self, structure_set: str, *, dimension: str | None = None
    ) -> Sourced | None:
        """到达某结构集的期望最近距离（格）。"""
        dimension = dimension or self.dimension
        for row in self.store.rows.values():
            if row.stat_type != "structure_density":
                continue
            if row.subject != structure_set or row.dimension != dimension:
                continue
            distance = row.value.get("expected_nearest_distance")
            if distance is None:
                continue
            detail = f"{structure_set} expected_nearest_distance（{row.scope_level}）"
            return Sourced(float(distance), "structure_density", detail)
        return None


    def travel(self, distance: float, *, underground: bool, underwater: bool = False) -> Sourced:
        """直线跑图的时间代价。

        地下与水下的系数同量级（都是约 2 倍）但成因不同：水下是移动速度
        （游泳 2.2 m/s 对步行 4.317），地下是速度加绕路。地下比水下更贵的
        那部分由竖井单独承担，不在这里叠加。
        """
        if underground:
            factor = gamedata.UNDERGROUND_MOVE_FACTOR
            why = "倍地下系数"
        elif underwater:
            factor = gamedata.UNDERWATER_MOVE_FACTOR
            why = "倍水下系数"
        else:
            factor = 1.0
            why = "倍地表系数"
        cost = distance * units.TU_PER_BLOCK_RUN * factor
        detail = f"{distance:.0f} 格 × {factor:g} {why}"
        return Sourced(cost, "travel", detail)


    def structure_water_share(self, structure_set: str) -> float | None:
        """某结构集合内水的体积占比。

        按**实例数**（`starts`）加权，不是等权：沉船的成员里有
        `shipwreck`（水下，286 个实例）和 `shipwreck_beached`（搁浅在沙滩，18 个），
        等权平均会得到 0.46 把整个集合判成陆地，而实际遇到的几乎都是水下那种。
        没有 `starts` 的行（模板行）按 1 计权。
        """
        members = _structure_maps().members_of(structure_set)
        if not members:
            return None
        shares: list[tuple[float, float]] = []
        for row in self.store.rows.values():
            if row.stat_type != "structure_content":
                continue
            if row.subject not in members:
                continue
            blocks = row.value.get("blocks_per_start") or {}
            total = sum(
                amount for amount in blocks.values() if isinstance(amount, (int, float))
            )
            if total <= 0:
                continue
            wet = sum(
                amount
                for name, amount in blocks.items()
                if name in WATER_BLOCKS and isinstance(amount, (int, float))
            )
            weight = row.value.get("starts")
            if not isinstance(weight, (int, float)) or weight <= 0:
                weight = 1.0
            shares.append((wet / total, float(weight)))
        if not shares:
            return None
        total_weight = sum(weight for _, weight in shares)
        return sum(share * weight for share, weight in shares) / total_weight


    def is_underwater_structure(self, structure_set: str) -> bool:
        """结构是否在水下：水占比超过 `gamedata.UNDERWATER_WATER_SHARE`。"""
        share = self.structure_water_share(structure_set)
        return share is not None and share >= gamedata.UNDERWATER_WATER_SHARE


    def block_dimension(self, block: str) -> str:
        """该方块在哪个维度有非零密度。主世界优先。

        下界矿石（`ancient_debris` / `nether_gold_ore`）在主世界群系下也有一行，
        但那是笛卡尔积的空行（密度 0）。按默认维度查会把它们判成「没有密度」，
        于是挖矿路线整条缺失——而这些方块只在下界才有。
        """
        cached = self._dimension_cache.get(("block", block))
        if cached is not None:
            return cached
        found: list[str] = []
        for row in self.store.of("ore_density", block) + self.store.of("block_density", block):
            if not row.dimension:
                continue
            density = row.value.get("per_chunk_density") or row.value.get("per_chunk") or 0.0
            if not isinstance(density, (int, float)) or density <= 0:
                continue
            if row.dimension not in found:
                found.append(row.dimension)
        result = found[0] if found else "minecraft:overworld"
        for dimension in found:
            if dimension == "minecraft:overworld":
                result = dimension
                break
        self._dimension_cache[("block", block)] = result
        return result


    def structure_dimension(self, structure_set: str) -> str:
        """某结构集合在哪个维度。**主世界优先**。

        `minecraft:ruined_portals` 在主世界与下界都有；下游默认从主世界出发，
        所以取主世界那一条。两个维度都没有时回退主世界（让调用方按缺数据处理）。
        """
        cached = self._dimension_cache.get(("structure", structure_set))
        if cached is not None:
            return cached
        found: list[str] = []
        for row in self.store.of("structure_density", structure_set):
            if not row.dimension:
                continue
            if row.dimension not in found:
                found.append(row.dimension)
        result = found[0] if found else "minecraft:overworld"
        for dimension in found:
            if dimension == "minecraft:overworld":
                result = dimension
                break
        self._dimension_cache[("structure", structure_set)] = result
        return result


    # 挖矿道前进 1 格扫过的体积（1 宽 × 2 高）。
    SEARCH_SCAN_VOLUME = 2.0


    def structure_search_cost(
        self,
        structure: str,
        *,
        volume_per_site: float,
        dimension: str | None = None,
        top_y: int | None = None,
        bottom_y: int | None = None,
        break_block: str | None = None,
    ) -> tuple[Sourced, Sourced] | None:
        """**挖矿道搜索**一处地下结构的代价（不是「知道位置直奔」）。

        `structure_nearest_distance` 给的是「最近一处」——等于假设玩家已经
        知道它在哪。地牢这类结构既不在图上标记、也没法从地表看见。

        **不靠「体积 ÷ 通道截面」**：那个换算对大厅必错（大厅体积大、穿过去
        只要几十格）。改用**体积占比**，只需要「每处结构的占地体积」：

            玩家前进 1 格扫过 `SEARCH_SCAN_VOLUME` 格³，
            命中概率 = 扫过体积 × (结构总体积 ÷ 地下总体积)，
            所以期望前进格数 = 地下总体积 ÷ (扫描体积 × 结构总体积)。

        `volume_per_site` 是每处结构的占地体积（格³），由调用方按版本事实给。
        返回 `(代价, 期望推进距离)`。
        """
        dimension = dimension or self.dimension
        content = self.structure_content(
            structure, source="measured", dimension=dimension
        )
        if content is None:
            return None, None
        starts = content[0].get("starts")
        if not isinstance(starts, (int, float)) or starts <= 0:
            return None, None
        if volume_per_site <= 0:
            return None, None
        per_chunk = self.underground_volume_per_chunk(
            dimension=dimension, top_y=top_y, bottom_y=bottom_y
        )
        chunks = self._scanned_chunks(dimension)
        if per_chunk <= 0 or chunks <= 0:
            return None, None
        total_volume = per_chunk * chunks
        structure_volume = float(starts) * volume_per_site
        if structure_volume <= 0:
            return None, None
        advance = total_volume / (self.SEARCH_SCAN_VOLUME * structure_volume)
        break_seconds = mining_facts.break_seconds(
            break_block or "stone", material=gamedata.UNDERGROUND_MINING_MATERIAL
        )
        dig = advance * self.SEARCH_SCAN_VOLUME * break_seconds / units.SECONDS_PER_TU
        walk = advance * units.TU_PER_BLOCK_RUN * gamedata.UNDERGROUND_MOVE_FACTOR
        cost = dig + walk
        detail = (
            f"{structure}：地下体积 {total_volume:.3g} 格³ ÷ (扫描 "
            f"{self.SEARCH_SCAN_VOLUME:g} 格³/格 × {starts:.0f} 处 × "
            f"{volume_per_site:g} 格³/处) = 期望挖 {advance:.0f} 格"
            f"（挖掘 {dig:.1f} + 走动 {walk:.1f} TU）"
        )
        return Sourced(cost, "structure_search", detail), Sourced(
            advance, "structure_search", f"{structure} 每处的期望推进距离"
        )


    def _scanned_chunks(self, dimension: str) -> int:
        """该维度的扫描区块数（来自逐纵段统计）。"""
        for row in self.store.rows.values():
            if (
                row.stat_type == "section_fill"
                and row.dimension == dimension
                and not row.biome
            ):
                value = row.value.get("chunks")
                if isinstance(value, (int, float)) and value > 0:
                    return int(value)
        return 0
