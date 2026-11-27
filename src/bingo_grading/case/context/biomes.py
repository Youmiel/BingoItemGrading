"""群系与搜索：斑块占比、找首片、晶洞与水源的到达代价。"""

from __future__ import annotations

import math

from bingo_grading import gamedata, paths
from .model import FISH_WATER_BIOMES, Sourced, _biome_feature_blocks

class BiomesMixin:
    def placed_by_biome_feature(self, block: str, *, pack: str | None = None) -> bool:
        """Whether a biome feature places this block. Only positive evidence of natural
        generation: tree decorators are not feature references. `False` when data is missing.
        """
        pack = pack or self.pack_version()
        worldgen = paths.worldgen_facts(pack)
        datapack = paths.datapack(pack)
        if not worldgen.exists or not datapack.exists:
            return False
        biome_dir = datapack.path / "minecraft" / "worldgen" / "biome"
        return block in _biome_feature_blocks(str(worldgen.path), str(biome_dir))

    def biome_spawn_weights(
        self, mob: str
    ) -> list[tuple[str, float, float, str]]:
        """群系刷怪表里出现在哪些群系：`[(群系, 权重, 同类别权重和, 类别)]`。

        只算 `biome:` 类 holder。用途同 `structure_spawn_weights()`：
        `w / W` 是「刷出来的是它」的机率，所以要拿一只平均要打死 `W / w` 只。

        **与结构不同的是「待多久」这一层没有数据**：结构里玩家是**在场**的，
        刷怪规则一直在跑；群系里玩家可能只是路过。这里的折中是把
        「等一只」当成固定周期（见 `gamedata.NATURAL_SPAWN_SECONDS`），
        并只统计**类别内**竞争——`monster` 与 `creature` 不共用刷怪名额。
        """
        mob_name = mob.split(":", 1)[-1]
        out: list[tuple[str, float, float, str]] = []
        for holder, rows in sorted(self._mob_spawn_index().items()):
            kind, _, biome = holder.partition(":")
            if kind != "biome" or not biome:
                continue
            target = None
            for entity, value in rows.items():
                if entity.split(":", 1)[-1] == mob_name:
                    target = value
                    break
            if target is None:
                continue
            weight = target.get("weight")
            if not isinstance(weight, (int, float)) or weight <= 0:
                continue
            category = str(target.get("category") or "")
            total = 0.0
            for value in rows.values():
                if str(value.get("category") or "") != category:
                    continue
                other = value.get("weight")
                if isinstance(other, (int, float)) and other > 0:
                    total += float(other)
            if total <= 0:
                continue
            out.append((biome, float(weight), total, category))
        return out


    def biome_search_cost(
        self, biome: str, *, dimension: str | None = None
    ) -> tuple[Sourced, str] | None:
        """走到该群系的期望距离：`(Sourced 代价, 该群系所在维度)`。

        与 `block_search_cost` 同一套面积占比法：
        `距离 ≈ ½·√(256 ÷ area_fraction)`。群系的 `area_fraction` 是**占总地表
        面积**的比例，所以分母直接用 256 格²。
        """
        best: tuple[float, str] | None = None
        for row in self.store.of_type("biome_patch"):
            if row.biome != biome:
                continue
            if dimension is not None and row.dimension != dimension:
                continue
            fraction = row.value.get("area_fraction")
            if not isinstance(fraction, (int, float)) or fraction <= 0:
                continue
            distance = 0.5 * math.sqrt(256.0 / fraction)
            if best is None or distance < best[0]:
                best = (distance, row.dimension)
        if best is None:
            return None
        distance, found_dimension = best
        underground = found_dimension != "minecraft:overworld"
        travel = self.travel(distance, underground=underground)
        detail = f"走到 {biome.split(':')[-1]}（{distance:.0f} 格）"
        return Sourced(travel.value, "biome_patch:search", detail), found_dimension


    def pale_garden_heart_reach(self) -> tuple[Sourced, str] | None:
        """走到一处 creaking_heart 的代价：先到苍白之园，再在群系里找有心的树。

        heart 只长在苍白之园的 pale oak 树顶（10% 的树带心），所以不能拿全图
        散点密度直接算——那会把「先找到群系」这一段漏掉。群系内密度按
        `heart 全图密度 ÷ 群系面积占比` 反推，两个数都来自快照。
        """
        biome = self.biome_search_cost("minecraft:pale_garden")
        if biome is None:
            return None
        fraction = None
        for row in self.store.of_type("biome_patch"):
            if row.biome != "minecraft:pale_garden":
                continue
            value = row.value.get("area_fraction")
            if isinstance(value, (int, float)) and value > 0:
                fraction = float(value)
                break
        density = None
        for row in self.store.of("block_density", "minecraft:creaking_heart"):
            # 只取全图行：群系级行是另一次扫描的口径，两者不能混用。
            if row.biome:
                continue
            value = row.value.get("per_chunk_density")
            if isinstance(value, (int, float)) and value > 0:
                density = float(value)
                break
        if fraction is None or density is None:
            return None
        inside = density / fraction
        distance = 0.5 * math.sqrt(256.0 / inside)
        walk = self.travel(distance, underground=False)
        detail = (
            f"{biome[1]}，再在群系里找带心的树 {distance:.0f} 格"
            f"（heart 全图 {density:.4f}/区块 ÷ 群系占比 {fraction:.4%} = {inside:.3f}/区块）"
        )
        return Sourced(biome[0].value + walk.value, "biome_patch+block_density", detail), detail


    def block_search_cost(
        self, block: str, *, dimension: str | None = None
    ) -> tuple[Sourced, str] | None:
        """走到最近一处该方块的期望距离：`(Sourced 代价, 说明)`。

        与 `water_search_cost` 同一套面积占比法：`距离 ≈ ½·√(256 ÷ 每区块个数)`。
        取 0.5 是因为「最近的斑块」通常在半个网格内。

        用于「看得见但稀有」的方块——蜂巢（森林 0.0048/区块，约每 208 区块一个，
        折算 115 格）就属于这类：不用挖穿岩层，但要专门去找。
        """
        dimension = dimension or self.dimension
        density = 0.0
        for row in self.store.rows.values():
            if row.stat_type != "block_density" or row.subject != block:
                continue
            if row.dimension != dimension:
                continue
            value = row.value.get("per_chunk_density")
            if isinstance(value, (int, float)) and value > density:
                density = float(value)
        if density <= 0:
            return None
        distance = 0.5 * math.sqrt(256.0 / density)
        found = self.travel(distance, underground=False)
        detail = (
            f"走到最近的 {block.split(':')[-1]}（{distance:.0f} 格；"
            f"每区块 {density:.4f} 个）"
        )
        return Sourced(found.value, "block_density:search", detail), detail


    def geode_harvest(
        self, *, entries_per_cluster: float = 4.0, dimension: str | None = None
    ) -> tuple[Sourced, str] | None:
        """一次紫水晶洞的收获：`(碎片数, 说明)`。

        `mine_vein` 那套「挖穿岩层、平均挖 1/ρ 块才碰到一块目标」对紫水晶是错的：
        洞是个球形空腔，挖到任意一块洞壁就意味着整窝都在手边。所以按「挖到第一块
        洞壁 → 采走这一窝」算，孔数靠实测密度：

            每洞成熟簇 = (cluster 密度 ÷ budding 密度) × `GEODE_BUDDING`

        两个密度都来自快照（`block_density`），只有 `GEODE_BUDDING` 是结构常量。
        实测 cluster 0.0539、budding 1.2616 → 每 23 块 budding 才有一颗长成，
        乘 30 得 **1.28 簇/洞**，按每簇 4 个碎片算是 **5.1 个碎片/洞**。

        芽挖掉不掉碎片（只有成熟簇掉），所以小/中/大芽不计收益。
        """
        dimension = dimension or self.dimension
        counts: dict[str, float] = {}
        for subject in ("minecraft:amethyst_cluster", "minecraft:budding_amethyst"):
            for row in self.store.rows.values():
                if row.stat_type != "block_density" or row.subject != subject:
                    continue
                if row.dimension != dimension:
                    continue
                density = row.value.get("per_chunk_density")
                if isinstance(density, (int, float)) and density > 0:
                    counts[subject] = float(density)
                    break
        cluster = counts.get("minecraft:amethyst_cluster")
        budding = counts.get("minecraft:budding_amethyst")
        if not cluster or not budding:
            return None
        ratio = cluster / budding
        clusters = ratio * gamedata.GEODE_BUDDING
        shards = clusters * entries_per_cluster
        detail = (
            f"每洞 {gamedata.GEODE_BUDDING:g} 块 budding × "
            f"成熟簇占比 {ratio:.2%}（{cluster:.4f}/{budding:.4f}）= "
            f"{clusters:.2f} 簇 × {entries_per_cluster:g} 个碎片 = {shards:.2f} 个/洞"
        )
        return Sourced(shards, "block_density:geode", detail), detail


    def water_search_cost(
        self, biomes: object = None
    ) -> tuple[Sourced, str] | None:
        """走到最近一处水域的期望距离：`(Sourced 代价, 用了哪个群系)`。

        用 `biome_patch` 的**面积占比**估：`到最近斑块的期望距离 ≈ ½·√(平均斑块面积 ÷ 面积占比)`。
        这和结构搜索同一个思路（占比法），但分母是二维面积而不是体积。

        `biomes` 传入该生物自带的栖息地（来源索引的 `biomes` 字段），只在这些群系里找：
        鲑鱼能在河里捞（15 格），河豚只能在暖/温海洋（110 格），差异很实在。
        不传就取所有水域群里最近的那个。

        为什么需要它：抓鱼（`code/mob_buckets`）、取水、钓鱼都要「有水的地方」，
        而鱼类没有 `mob_density`（C17 撤回后不再采生物密度），
        不另给一个「找水」的成本，鱼桶就只能靠「跟渔夫买」。

        这是**估计值**：只保证量级（十几格到两百格），不保证精度。
        """
        wanted = {
            biome if biome.startswith("minecraft:") else f"minecraft:{biome}"
            for biome in (biomes or ())
            if isinstance(biome, str)
        }
        allowed = FISH_WATER_BIOMES & wanted if wanted else FISH_WATER_BIOMES
        best: tuple[float, str, float, float] | None = None
        for row in self.store.rows.values():
            if row.stat_type != "biome_patch":
                continue
            if row.biome not in allowed:
                continue
            fraction = row.value.get("area_fraction")
            area = row.value.get("mean_area")
            if not isinstance(fraction, (int, float)) or fraction <= 0:
                continue
            if not isinstance(area, (int, float)) or area <= 0:
                continue
            distance = 0.5 * math.sqrt(area / fraction)
            if best is None or distance < best[0]:
                best = (distance, row.biome, float(fraction), float(area))
        if best is None:
            return None
        distance, biome, fraction, area = best
        found = self.travel(distance, underground=False)
        detail = (
            f"走到最近水域（{biome.split(':')[-1]}，"
            f"{distance:.0f} 格；该类群系占 {fraction * 100:.1f}% 面积、平均斑块 {area:.0f}）"
        )
        return Sourced(found.value, "biome_patch:water_search", detail), biome
