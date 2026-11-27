"""配方与工具事实：比例、折算、最弱合格工具、熔炼。"""

from __future__ import annotations


from bingo_grading import paths
from bingo_grading import gamedata, units
from bingo_grading.recipes import facts
from .model import Sourced, _harvest_rows, _registry_entries

class RecipesMixin:
    def item_registered(self, item: str, *, pack: str | None = None) -> bool | None:
        """Whether the item registry has this item; `None` if the registry is unavailable."""
        resolved = paths.registry_facts(pack or self.pack_version())
        if not resolved.exists:
            return None
        entries = _registry_entries(str(resolved.path), "minecraft:item")
        return None if entries is None else item in entries

    def recipe_ratio(
        self,
        output: str,
        ingredient: str,
        *,
        serializer: str | None = None,
        pack: str | None = None,
    ) -> Sourced | None:
        """每 1 个 `ingredient` 能做出几个 `output`（上游配方事实）。

        折算比例是版本事实，不硬编码。缺文件或缺该配方时返回 `None`，
        调用方必须让相关路线缺席，不能填默认值。
        """
        index, resolved = self._recipe_index(pack)
        if index is None:
            return None
        value = index.ratio(output, ingredient, serializer=serializer)
        if value is None:
            return None
        recipe = index.recipe_for(output, ingredient, serializer=serializer)
        detail = (
            f"每 1 个 {ingredient} 出 {value:.4g} 个 {output}"
            f"（一次配方 {recipe.count:g} 个，用 {dict(recipe.inputs)[ingredient]:g} 个原料；"
            f"{recipe.serializer or 'recipe'}，{recipe.source_file}）"
        )
        return Sourced(value, f"recipe:{resolved.layer}", detail)


    def recipe_conversion(
        self,
        output: str,
        *,
        via: str | None = None,
        reverse: bool = False,
        serializer: str | None = None,
        pack: str | None = None,
    ) -> tuple[facts.Conversion, str] | None:
        """一条完整的配方折算：产出数量 + 全部原料用量（来自上游配方事实）。

        合成关系不是一比一（桶要 3 铁、打火石要铁 + 燧石、一次配方出 3 个火焰弹），
        所以原料用量也必须读配方，不能在案例层写 `Requirement(x, 1.0)`。

        `reverse=True` 把成品拆开（1 干草块 → 9 小麦）。缺数据时返回 `None`。
        """
        index, resolved = self._recipe_index(pack)
        if index is None:
            return None
        conversion = index.conversion(
            output, via=via, reverse=reverse, serializer=serializer
        )
        if conversion is None:
            return None
        return conversion, f"recipe:{resolved.layer}"


    def recipe_conversions(
        self,
        output: str,
        *,
        serializer: str | None = None,
        pack: str | None = None,
    ) -> tuple[tuple[facts.Conversion, ...], str]:
        """同一产出的**全部**配方折算。

        一个物品常有多条配方（铁锭：熔炼矿石 / 爆烤矿石 / 拆铁块 / 拆粒），
        成本和是否可行差别很大，必须交给成本模型比较，不能只取第一条。
        """
        index, resolved = self._recipe_index(pack)
        if index is None:
            return (), ""
        return index.conversions(output, serializer=serializer), f"recipe:{resolved.layer}"


    def harvest_tools(
        self, block: str, *, pack: str | None = None
    ) -> tuple[list[str], str]:
        """全部最弱合格工具（同级可能有多把，如石镐与铜镐），以及出处说明。

        返回上游原序；要「实际用哪一把」走 `harvest_tool()`，那里会避开铜制工具。
        没有工具要求或数据缺失时返回 `([], "")`。
        """
        resolved = paths.harvest_facts(pack or self.pack_version())
        if not resolved.exists:
            return [], ""
        row = _harvest_rows(str(resolved.path)).get(block)
        if not row:
            return [], ""
        tools = [str(tool) for tool in row.get("tools") or ()]
        if not tools:
            return [], ""
        level = row.get("tool_level") or "?"
        detail = (
            f"{block} 最低工具等级 {level}；最弱合格工具 {'、'.join(tools)}"
            f"（harvest:{resolved.layer}）"
        )
        return tools, detail


    def harvest_tool(
        self, block: str, *, pack: str | None = None
    ) -> tuple[str, str] | None:
        """挖这个方块所需的最弱合格工具：`(物品 ID, 出处)`。

        数据来自 RA 导出的 `harvest.jsonl`（`minecraft:tool` 组件 +
        `needs_*_tool` 标签）。没有工具要求或数据缺失时返回 `None`。

        同级有多把时避开铜制工具：`harvest.jsonl` 对 stone 级方块同时给出
        `copper_pickaxe` 与 `stone_pickaxe`，而铜镐要先把铜挖出来才做得成，
        拿它当前置会让 `copper_pickaxe ← copper_ore ← copper_pickaxe` 成环，
        整条挖矿路线被抬到箱子路线之上。实际开矿时没人会专门做铜镐，
        石镐挖得动同样的方块。其余候选保持上游顺序。
        """
        tools, detail = self.harvest_tools(block, pack=pack)
        if not tools:
            return None
        affordable = [tool for tool in tools if "copper_" not in tool]
        return (affordable or tools)[0], detail


    def harvest_level(self, block: str, *, pack: str | None = None) -> str | None:
        """方块的最低工具等级（stone / iron / diamond）；无要求时为 `None`。"""
        resolved = paths.harvest_facts(pack or self.pack_version())
        if not resolved.exists:
            return None
        row = _harvest_rows(str(resolved.path)).get(block)
        return (row or {}).get("tool_level") or None


    def _recipe_index(self, pack: str | None = None):
        pack = pack or self.pack_version()
        if pack not in self._recipe_cache:
            self._recipe_cache[pack] = self._load_recipe_index(pack)
        return self._recipe_cache[pack]


    def _load_recipe_index(self, pack: str):
        resolved = paths.recipe_facts(pack)
        if not resolved.exists:
            return None, None
        stations = paths.serializer_stations(pack)
        tags = paths.tag_facts(pack)
        try:
            index = facts.load(
                str(resolved.path),
                str(stations.path) if stations.exists else None,
                str(tags.path) if tags.exists else None,
            )
        except facts.RecipeFactError:
            return None, None
        return index, resolved


    # ---------------------------------------------------------------- 熔炼

    def smelt(
        self,
        count: float,
        *,
        fuel_subject: str = "minecraft:coal_ore",
        fuel_items_per_smelt: float = 1.0 / gamedata.SMELTS_PER_FUEL,
        dimension: str | None = None,
    ) -> tuple[Sourced, list[str]]:
        """熔炼 `count` 个物品的代价：冶炼时间 + 燃料折算。

        燃料按「挖煤」计。煤的密度极高且分布很广，这部分代价很小但并非为零；
        缺煤数据时退化为只算冶炼时间，并返回一条假设说明。

        返回 (cost, assumptions)。
        """
        assumptions: list[str] = []
        cost = count * gamedata.SMELT_SECONDS / units.SECONDS_PER_TU

        fuel_needed = count * fuel_items_per_smelt
        fuel_estimate, fuel_sourced = self.mining(
            fuel_subject, dimension=dimension, target_items=fuel_needed
        )
        if fuel_estimate is not None and fuel_sourced is not None:
            cost += fuel_sourced.value
            detail = (
                f"冶炼 {count:g} 个：{gamedata.SMELT_SECONDS:g}s/个；"
                f"燃料 {fuel_needed:.2f} 份（{fuel_sourced.detail}）"
            )
        else:
            assumptions.append(
                f"缺 {fuel_subject} 的密度统计，燃料代价未计入（漏算部分很小）"
            )
            detail = f"冶炼 {count:g} 个：{gamedata.SMELT_SECONDS:g}s/个，燃料未计"
        return Sourced(cost, "smelt", detail), assumptions
