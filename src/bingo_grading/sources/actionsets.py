"""全图动作产物：`build/<pack>/actions.jsonl`。

一次构建出来的动作图落盘，之后查询只读盘、不重建。头部记**构建输入指纹**，
统计快照、配方事实、手工知识表、来源索引任何一样变了，落盘的图就作废。

`budget` 随图落盘。`solve` 的层数是整张依赖图的 `max_level + 2`，同一个物品
在不同层数下可能差几个百分点（段数跨过枚举上限时 `_pick` 切近似分支），
所以读盘后固定用落盘值，不按查询目标重算——否则同一件物品每次查询都可能不同。

## 为什么指纹算文件而不是算「构建参数」

图的每条成本都来自读进来的数据文件，参数只是选择读哪些文件。文件内容变了
（例如 `stats/` 里改了一个数值、`knowledge/` 改了一条概率），图就得重建，
而参数字符串不会有任何变化。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from bingo_grading import paths
from bingo_grading.plan import Action, ActionSet

ACTIONS_SCHEMA = 1

# 落盘图的**构建策略**版本。`ACTIONS_SCHEMA` 只管文件结构（增删字段），而
# 「同一份输入、构建逻辑变了、产出的图不同」由这个数表达：它参与
# `digest_inputs`，所以策略一变，所有旧图自动判过期并要求重建。
#
# 下列改动要 +1：`sources/actions/*` 的动作生成、`case/context/*` 的取值口径、
# 全库 targets 的取法（例如把纯配方物品纳入）。只改求解/计价（`plan/*`、
# `grade.py`）不影响落盘图，不用动。
BUILD_POLICY = 1  # 1：2026-11-27 引入，初始值

# 头部里用到的键。写盘与读盘共用，避免两处各写一遍。
_HEADER_KEYS = (
    "actions_schema",
    "label",
    "pack_version",
    "stats_digest",
    "actions",
    "targets",
    "budget",
    "sources",
    "uncovered",
    "no_action_items",
    "no_action_examples",
    "missing_prereq_items",
    "missing_prereqs",
)


class ActionsFileError(ValueError):
    """落盘的动作图不可用：schema 不符、指纹过期。"""


class ActionsFileMissing(ActionsFileError):
    """产物还不存在。`grade --all` 据此决定「首次自动构建」，其余场景一律报错。"""


def default_path(pack: str) -> Path:
    return paths.BUILD_ROOT / pack / "actions.jsonl"


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _relative(path: Path) -> str:
    """指纹里只用相对路径：绝对路径带用户名，不该进产物。"""
    try:
        return path.relative_to(paths.ROOT).as_posix()
    except ValueError:
        return path.name


def digest_inputs(*, pack: str, index_path: Path, stats_path: Path) -> str:
    """图所依赖的全部输入文件的指纹。

    目录只取 `*.jsonl`（统计快照是多文件快照），文件直接算。
    缺失的文件也进指纹：数据补齐之后必须重建，不能沿用旧的「缺失」结论。
    """
    candidates: list[Path] = [Path(index_path), Path(stats_path)]
    if Path(stats_path).is_dir():
        candidates.extend(sorted(Path(stats_path).glob("*.jsonl")))
        candidates.append(Path(stats_path) / "meta.json")
    # 上游可达性图直接提供方块变换（`transmutes` 边），配方事实提供折算比例。
    candidates.append(Path(paths.recipe_analysis(pack).path) / "graph.json")
    for resolved in (
        paths.recipe_facts(pack),
        paths.harvest_facts(pack),
        paths.tag_facts(pack),
        paths.template_facts(pack),
        paths.serializer_stations(pack),
    ):
        candidates.append(Path(resolved.path))
    candidates.extend(sorted(paths.KNOWLEDGE_ROOT.glob("*.toml")))

    hasher = hashlib.sha256()
    # 构建策略也算输入：同一批文件配上不同的生成逻辑，产出的图不是同一张。
    hasher.update(f"build_policy={BUILD_POLICY}".encode("ascii"))
    hasher.update(b"\0")
    for path in candidates:
        hasher.update(_relative(Path(path)).encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(_sha256(Path(path)).encode("ascii") if Path(path).is_file() else b"<missing>")
        hasher.update(b"\0")
    return hasher.hexdigest()


@dataclass(frozen=True)
class GraphFile:
    """一张落盘的动作图，以及构建它的口径。"""

    header: dict
    actionset: ActionSet

    @property
    def budget(self) -> int:
        return int(self.header["budget"])

    @property
    def digest(self) -> str:
        return str(self.header.get("stats_digest", ""))

    @property
    def sources(self) -> dict[str, int]:
        """每件物品在来源索引里的来源条数（`grade` 的 `sources` 列）。"""
        return dict(self.header.get("sources") or {})

    @property
    def uncovered(self) -> dict[str, int]:
        return dict(self.header.get("uncovered") or {})

    @property
    def no_action_examples(self) -> list[str]:
        """有来源记录、但图上没有动作的物品（最多 10 个样例）。"""
        return list(self.header.get("no_action_examples") or ())

    @property
    def missing_prereqs(self) -> dict[str, int]:
        """前置指向不存在的物品：`物品 → 引用次数`。

        这类动作会被 `tiers` 判成 `no_rule` 后静默丢弃，所以构建时就算好、
        落进头部，`bg doctor` 据此报警（非空就是 bug：名字拼错或需求未接）。
        """
        return dict(self.header.get("missing_prereqs") or {})

    @property
    def targets(self) -> list[str]:
        """构建时覆盖的目标物品（= 来源索引里的全部物品）。"""
        return sorted(self.sources)

    def subgraph(self, target: str) -> ActionSet:
        """只保留产出 `target` 及其前置链的那部分动作。

        全图 4481 条动作求解一次约 5 秒，单件查询不该付这个代价；而
        `solve` 的层数与 `budget` 固定，裁掉与目标无关的动作不改变目标的
        曲线——每个动作的定价只看它自己前置的上一轮曲线。
        """
        return subgraph(self.actionset, target)


def subgraph(actionset: ActionSet, target: str) -> ActionSet:
    """反向可达：产出 `target` 的动作，加上它们前置的动作。"""
    producers: dict[str, list] = {}
    for action in actionset.actions:
        for item in action.yields:
            producers.setdefault(item, []).append(action)
    keep: list = []
    seen_actions: set[str] = set()
    seen_items: set[str] = set()
    frontier = [target]
    while frontier:
        item = frontier.pop()
        if item in seen_items:
            continue
        seen_items.add(item)
        for action in producers.get(item, ()):
            if action.action_id in seen_actions:
                continue
            seen_actions.add(action.action_id)
            keep.append(action)
            for requirement in action.requires:
                if requirement.item not in seen_items:
                    frontier.append(requirement.item)
    return ActionSet(label=f"{actionset.label}#{target}", actions=tuple(keep))


def write(
    path: Path,
    actionset: ActionSet,
    *,
    pack: str,
    budget: int,
    sources: dict[str, int],
    uncovered: dict[str, int],
    stats_digest: str,
    no_action_items: list[str] | None = None,
    missing_prereqs: dict[str, int] | None = None,
) -> dict:
    """按 `action_id` 排序写出，两次构建逐字节一致。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {
        "actions_schema": ACTIONS_SCHEMA,
        "label": actionset.label,
        "pack_version": pack,
        "stats_digest": stats_digest,
        "actions": len(actionset.actions),
        "targets": len(sources),
        "budget": budget,
        "sources": dict(sorted(sources.items())),
        "uncovered": dict(sorted(uncovered.items())),
        "no_action_items": len(no_action_items or ()),
        "no_action_examples": list(no_action_items or ())[:10],
        "missing_prereq_items": len(missing_prereqs or ()),
        "missing_prereqs": dict(sorted((missing_prereqs or {}).items())),
    }
    lines = [json.dumps(header, ensure_ascii=False, sort_keys=True)]
    for action in sorted(actionset.actions, key=lambda item: item.action_id):
        # `full=True`：落盘的图必须逐位往返，舍入会让 `whole_cost_tu` 漂移。
        lines.append(json.dumps(action.as_dict(full=True), ensure_ascii=False, sort_keys=True))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return header


def read(path: Path, *, expect_digest: str | None = None) -> GraphFile:
    """读回落盘图。`expect_digest` 给出时校验输入指纹。"""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ActionsFileMissing(
            f"动作图不存在：{path}；先跑 `bg index actions` 生成"
        ) from exc
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ActionsFileMissing(f"{path} 是空文件；先跑 `bg index actions` 重新生成")
    header = json.loads(lines[0])
    if header.get("actions_schema") != ACTIONS_SCHEMA:
        raise ActionsFileError(
            f"{path} 的 actions_schema={header.get('actions_schema')!r}，"
            f"本程序只认 {ACTIONS_SCHEMA}；跑 `bg index actions` 重新生成"
        )
    missing = [key for key in _HEADER_KEYS if key not in header]
    if missing:
        raise ActionsFileError(f"{path} 的头部缺字段 {missing}；重新生成")
    if expect_digest is not None and header["stats_digest"] != expect_digest:
        raise ActionsFileError(
            f"{path} 已过期：构建输入（统计快照 / 配方事实 / 知识表 / 来源索引）"
            "在本图落盘之后改过；跑 `bg index actions` 重新生成"
        )
    actionset = ActionSet(
        label=str(header["label"]),
        actions=tuple(Action.from_dict(json.loads(line)) for line in lines[1:]),
    )
    if len(actionset.actions) != header["actions"]:
        raise ActionsFileError(
            f"{path} 头部写 {header['actions']} 个动作，实际读到 "
            f"{len(actionset.actions)} 条；重新生成"
        )
    return GraphFile(header=header, actionset=actionset)
