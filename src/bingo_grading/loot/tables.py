"""分层加载战利品表：原版打底，地形包覆盖。

地形包是数据包，`data/<ns>/loot_table/**` 与原版同路径即覆盖。版本分层包
（`26_3_loot/`）在原版包之上再覆盖一层，所以加载顺序是：

```
原版 data/  →  地形包 26_<minor>_loot/  →  地形包 data/
```

后面的覆盖前面的。不合并字段，只按「同 ID 后写覆盖」处理——Minecraft 的
数据包语义就是整文件替换，不是深合并。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from mcshared.loot import Table, normalize_table

LOOT_DIR = "loot_table"
LOOT_DIRS = (LOOT_DIR, "loot_tables")


class LootTableError(ValueError):
    """战利品表不可用。"""


@dataclass
class LootTableSet:
    """按 ID 索引的战利品表集合。`tables` 的键是 `minecraft:chests/simple_dungeon`。

    存的是**归一后的** `mcshared.loot.Table`，不是原始 JSON：方言差异
    （`condition` / `conditions`、`modifier` / `functions`）在加载时一次性消掉，
    期望计算不再关心数据包版本。
    """

    tables: dict[str, Table] = field(default_factory=dict)
    # 每个 ID 来自哪个根目录，用于溯源与排查覆盖。
    sources: dict[str, Path] = field(default_factory=dict)
    # 解析失败的路径，不静默跳过。
    broken: dict[Path, str] = field(default_factory=dict)

    def get(self, table_id: str) -> Table | None:
        return self.tables.get(table_id)

    def require(self, table_id: str) -> Table:
        table = self.tables.get(table_id)
        if table is None:
            raise LootTableError(f"战利品表不存在：{table_id}")
        return table

    def ids(self) -> list[str]:
        return sorted(self.tables)

    def __len__(self) -> int:
        return len(self.tables)

    def __contains__(self, table_id: object) -> bool:
        return table_id in self.tables


def _table_id(namespace: str, root: Path, path: Path) -> str:
    """`<root>/minecraft/loot_table/chests/simple_dungeon.json` → `minecraft:chests/simple_dungeon`。"""
    relative = path.relative_to(root).with_suffix("")
    return f"{namespace}:{relative.as_posix()}"


def load(dirs: list[Path]) -> LootTableSet:
    """按顺序加载多个数据包根目录。后加载的覆盖先加载的。

    每个 `dir` 可以是：
    - 数据包根（含 `data/` 与可选的 `pack.mcmeta`）
    - `data/` 目录本身
    - `<ns>/loot_table/` 目录本身（此时命名空间取该目录的父目录名）

    覆盖语义是**整文件替换**，与 Minecraft 数据包一致，不做字段合并。
    """
    result = LootTableSet()
    for directory in dirs:
        directory = Path(directory)
        if not directory.is_dir():
            continue
        for namespace, root in _namespace_roots(directory):
            for path in sorted(root.rglob("*.json")):
                table_id = _table_id(namespace, root, path)
                try:
                    doc = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    result.broken[path] = str(exc)
                    continue
                if not isinstance(doc, dict):
                    result.broken[path] = "顶层不是对象"
                    continue
                result.tables[table_id] = normalize_table(doc, table_id)
                result.sources[table_id] = path
    if not result.tables:
        raise LootTableError(f"没有加载到任何战利品表：{dirs}")
    return result


def _namespace_roots(directory: Path) -> list[tuple[str, Path]]:
    """找出 `<directory>` 下所有「命名空间 → loot_table 目录」。"""
    data_dirs: list[Path] = []
    if directory.name == "data" and directory.is_dir():
        data_dirs.append(directory)
    elif (directory / "data").is_dir():
        data_dirs.append(directory / "data")

    if data_dirs:
        out: list[tuple[str, Path]] = []
        for data_dir in data_dirs:
            for namespace_dir in sorted(child for child in data_dir.iterdir() if child.is_dir()):
                for name in LOOT_DIRS:
                    loot = namespace_dir / name
                    if loot.is_dir():
                        out.append((namespace_dir.name, loot))
                        break
        if out:
            return out
        # 有 `data/` 结构但没有任何 `loot_table/`：这个包压根不含战利品表。
        # 典型例子是地形包里只覆盖 worldgen 的 overlay（如 `26_3_overworld`）。
        # 这里必须直接返回空，不能掉进下面「把整个目录当表」的兜底——否则
        # density_function / noise_settings 之类的 json 会被当成战利品表加载。
        return []

    # 直接给了 `<ns>/loot_table/`
    if directory.name in LOOT_DIRS and directory.parent.is_dir():
        return [(directory.parent.name, directory)]

    # 兜底：把目录当成某个命名空间下的 loot_table
    return [(directory.parent.name or "minecraft", directory)]
