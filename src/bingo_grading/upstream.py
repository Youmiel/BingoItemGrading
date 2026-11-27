"""上游产物本地化：同步与校验。

设计目标是让本项目**不再依赖上游项目的 junction**。做法是把上游产物整包复制
进 `data/upstream/`，并留一份 manifest 记录来源与校验和：

- `data/upstream/<pack>/recipe-analysis/`（RA 的 `output/<pack>/` 整棵）
- `data/upstream/<pack>/recipe-knowledge/`（配方知识表）
- `data/upstream/<pack>/worldgen-stats/<timestamp>/`（WGS 的一次运行，整目录）
- `data/upstream/<pack>/datapack/data/`（**合并后的数据包视图**：原版 ⊗ 地形包）
- `data/upstream/<pack>/datapack/data/minecraft/structure/`（结构模板，上游合并包没导）

manifest 的价值不只是「校验没坏」，更在于**上游重跑后能看出哪些事实变了**：
`git diff data/` 会显示具体哪个 `facts/*.jsonl` 或统计文件发生变动，
而不是只能重跑一遍然后凭印象判断。

只做「整包取」，不做文件级过滤：过滤会把「上游本来有、但被我们排除」和
「上游真没产出」混成同一个现象，排查时无从区分。

不复制的东西：`server.jar`、反编译源码、`assets`、世界存档、`node_modules`。
那些占了上游项目体积的绝大部分，本项目一点也用不到。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import paths

MANIFEST_NAME = "manifest.json"
# 4：所有同步项改为**整包**复制，不再按文件名或后缀过滤。
#    `loot-tables` 扩成整棵 `datapack/`（上游合并后的完整 data/，5.8 MB），
#    `worldgen-stats` 也不再只取 `.json`。
#
# 为什么不做过滤：过滤会把「上游本来有、但被我们排除」和「上游真的没有」
# 混成同一个现象，排查时无从区分。多拿几十 MB 换「上游给的都在」，这笔账划得来。
MANIFEST_SCHEMA = 4


@dataclass
class SyncEntry:
    """manifest 里的一条来源记录。"""

    kind: str
    source: str
    target: str
    layer: str
    files: int = 0
    bytes: int = 0
    digest: str = ""

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "source": self.source,
            "target": self.target,
            "layer": self.layer,
            "files": self.files,
            "bytes": self.bytes,
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "SyncEntry":
        return cls(
            kind=raw.get("kind", ""),
            source=raw.get("source", ""),
            target=raw.get("target", ""),
            layer=raw.get("layer", ""),
            files=int(raw.get("files") or 0),
            bytes=int(raw.get("bytes") or 0),
            digest=raw.get("digest", ""),
        )


@dataclass
class SyncReport:
    pack: str
    entries: list[SyncEntry] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def totals(self) -> dict:
        """按 target 去重后的文件数与字节数。

        `datapack/data` 这一条已经涵盖了 `datapack/data/minecraft/structure`
        （同一步的两半），直接相加会把结构模板算两遍。
        """
        targets = [Path(entry.target) for entry in self.entries]
        files = 0
        size = 0
        for entry, target in zip(self.entries, targets):
            covered = any(
                other != target and target.is_relative_to(other) for other in targets
            )
            if covered:
                continue
            files += entry.files
            size += entry.bytes
        return {"files": files, "bytes": size}

    def as_dict(self) -> dict:
        return {
            "pack": self.pack,
            "entries": [entry.as_dict() for entry in self.entries],
            "skipped": self.skipped,
            "removed": self.removed,
            "totals": self.totals,
        }


def _tree_digest(root: Path) -> tuple[int, int, str]:
    """目录的 (文件数, 字节数, 内容摘要)。摘要与文件顺序无关，可跨平台复现。"""
    files = sorted(p for p in root.rglob("*") if p.is_file())
    total = 0
    hasher = hashlib.sha256()
    for path in files:
        data = path.read_bytes()
        total += len(data)
        hasher.update(path.relative_to(root).as_posix().encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(hashlib.sha256(data).digest())
    return len(files), total, hasher.hexdigest()


def _copy_tree(
    source: Path, target: Path, *, keep_extra: tuple[str, ...] = ()
) -> tuple[int, int]:
    """**镜像**复制整个目录，返回 (文件数, 字节数)。

    复制完后会删掉 target 里不在源中的旧文件 —— 否则上一次同步留下的（或
    上游已不再产出的）文件会与 manifest 记录不符，`bg data verify` 随即报
    摘要不符。

    `keep_extra` 是相对路径前缀白名单，镜像清理时跳过它们：数据包视图里
    `minecraft/structure` 是另一步补进来的，不属于 `datapack-merged` 的源目录。
    """
    target.mkdir(parents=True, exist_ok=True)
    files = 0
    total = 0
    kept: set[Path] = set()
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        kept.add(destination.resolve())
        files += 1
        total += path.stat().st_size

    # 清掉镜像外的残留（`keep_extra` 指定的子树例外）
    def _preserved(rel: str) -> bool:
        return any(rel == item or rel.startswith(item + "/") for item in keep_extra)

    for path in sorted(target.rglob("*"), reverse=True):
        relative = path.relative_to(target).as_posix()
        if _preserved(relative):
            continue
        if path.is_file() and path.resolve() not in kept:
            path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    return files, total


def _git_head(repo: Path) -> str:
    """上游仓库的 HEAD commit；不是 git 仓库或没有 git 时返回空串。"""
    if not (repo / ".git").exists():
        return ""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _entry(kind: str, resolved: paths.Resolved, target: Path, copied: tuple[int, int]) -> SyncEntry:
    """manifest 的一条记录。

    `files` / `bytes` 取 **target 目录的实际统计**，而不是本次复制的数量：
    同一个 target 可能由多步共同填满（`datapack/data` 就是合并包 + 结构模板
    两步），只记复制量会与目录实际内容不符，`bg data verify` 随即报错。
    """
    files, total = copied
    digest = ""
    if target.exists():
        files, total, digest = _tree_digest(target)
    return SyncEntry(
        kind=kind,
        source=str(resolved.path),
        target=str(target),
        layer=resolved.layer,
        files=files,
        bytes=total,
        digest=digest,
    )


# ------------------------------------------------------------------ 同步


def sync(pack: str, *, dry_run: bool = False) -> SyncReport:
    """把四类数据从上游复制进 `data/`。逐类处理，缺哪类就跳过并记下。

    源一律取 `paths.upstream_*`（只认上游层），因此重复执行会真的重新同步，
    而不是把上次的快照当源再拷一遍。
    """
    report = SyncReport(pack=pack)

    # 1. 上游 RecipeAnalysis 产物（整目录：facts、graph、台账、可视化图一概不论）
    ra = paths.upstream_recipe_analysis(pack)
    if ra.exists:
        target = paths.UPSTREAM_ROOT / pack / "recipe-analysis"
        copied = (0, 0) if dry_run else _copy_tree(ra.path, target)
        if dry_run:
            copied = _tree_digest(ra.path)[:2]
        report.entries.append(_entry("recipe-analysis", ra, target, copied))
    else:
        report.skipped.append(f"recipe-analysis: 未找到 {ra.path}")

    # 1b. 上游配方知识表：serializer → 工艺设备。
    # 它在 RA 仓库的 knowledge 下、不在 output/ 里，但下游配方 IR 需要它，
    # 所以一并本地化，避免下游直接读 ref 或自己维护一份映射。
    vocabulary = paths.upstream_recipe_vocabulary()
    if vocabulary.exists:
        # 放在 `recipe-analysis/` 之外：那个目录整体由 manifest 校验，
        # 嵌进去会让它的文件数/摘要与记录不符。
        target = paths.UPSTREAM_ROOT / pack / "recipe-knowledge"
        copied = (0, 0) if dry_run else _copy_tree(vocabulary.path, target)
        if dry_run:
            copied = _tree_digest(vocabulary.path)[:2]
        report.entries.append(_entry("recipe-knowledge", vocabulary, target, copied))
    else:
        report.skipped.append(f"recipe-knowledge: 未找到 {vocabulary.path}")

    # 2. 上游采集侧统计运行（整目录，不做后缀过滤）
    run = paths.upstream_collector_run(pack)
    if run.exists:
        target = paths.UPSTREAM_ROOT / pack / "worldgen-stats" / run.path.name
        _prune_siblings(target, keep=run.path.name, report=report, dry_run=dry_run)
        copied = (0, 0) if dry_run else _copy_tree(run.path, target)
        if dry_run:
            copied = _tree_digest(run.path)[:2]
        report.entries.append(_entry("worldgen-stats", run, target, copied))
    else:
        report.skipped.append(f"worldgen-stats: 未找到运行目录（{run.path}）")

    # 3. 合并后的完整数据包：上游已把 datapack 覆盖叠好，这里整棵 `data/` 拿过来
    #    （原作版 + 地形包 overlay 的生效结果，共 8 千多个文件但只有几 MB）。
    #    本侧只用其中的 `minecraft/loot_table`，其余（worldgen 参数等）现在不读
    #    但**照样留着**——上游给了什么就是什么，不预设将来不需要。
    datapack = paths.upstream_datapack_merged(pack)
    if datapack.exists:
        target = paths.UPSTREAM_ROOT / pack / "datapack" / "data"
        # `minecraft/structure` 由下一步补进来，镜像清理时要保住它。
        copied = (
            (0, 0)
            if dry_run
            else _copy_tree(
                datapack.path, target, keep_extra=("minecraft/structure", "minecraft/structures")
            )
        )
        if dry_run:
            copied = _tree_digest(datapack.path)[:2]
        datapack_target, datapack_copied = target, copied
    else:
        report.skipped.append(
            f"datapack-merged: 未找到 {datapack.path}"
            "（先跑 `ra inputs init` 生成 datapack-merged/）"
        )

    # 4. 结构模板（SNBT）：形状匹配要方块坐标，`facts/` 只有计数。
    #    上游合并包没导 `structure/`（那是 datagen 从 jar 转出的缓存），这里补进
    #    同一个数据包视图，让它和 `loot_table` 一样待在 `data/minecraft/` 下。
    shapes = paths.upstream_vanilla_structures(pack)
    if shapes.exists:
        name = "structure" if (shapes.path / "minecraft" / "structure").is_dir() else "structures"
        source = shapes.path / "minecraft" / name
        target = paths.UPSTREAM_ROOT / pack / "datapack" / "data" / "minecraft" / name
        copied = (0, 0) if dry_run else _copy_tree(source, target)
        if dry_run:
            copied = _tree_digest(source)[:2]
        report.entries.append(
            _entry("structure-templates", paths.Resolved(source, shapes.layer), target, copied)
        )
    else:
        report.skipped.append(f"structure-templates: 未找到 {shapes.path}")

    if datapack.exists:
        # Structure templates land in the same directory; record the digest after both steps.
        report.entries.append(_entry("datapack-merged", datapack, datapack_target, datapack_copied))

    # 地形包原件与 `version.json` 都不再同步：overlay 判定与逐层覆盖已由上游完成
    # （见第 3 步），本项目不需要格式号，也没有任何代码再读 `data/packs/`。

    if not dry_run and report.entries:
        _write_manifest(pack, report)
    return report


def _prune_siblings(target: Path, *, keep: str, report: "SyncReport", dry_run: bool) -> None:
    """清掉 `target` 的同级旧目录，只留 `keep`。

    目前只有采集侧的统计运行存在「多份历史副本」的问题：`collector_run()` 取
    目录名最大的那个，旧 run 永远读不到，却会一直堆在 `data/upstream/` 里占地方。
    `target` 本身就由 manifest 校验，只留同步进去的那一份。
    """
    parent = target.parent
    if not parent.is_dir():
        return
    for stale in sorted(parent.iterdir()):
        if not stale.is_dir() or stale.name == keep:
            continue
        report.removed.append(stale.name)
        if not dry_run:
            shutil.rmtree(stale)


# ------------------------------------------------------------------ 前置检查
#
# `inputs/<pack>/` 与 `.cache/server-*/structures/` **不是** RecipeAnalysis 仓库里
# 常驻的东西：前者由 `ra inputs init` 从 server.jar 解包派生，后者是 datagen 的
# 中间产物；`output/<pack>/` 则要等 `ra ingest` + `ra run` 跑完才有。缺任何一项，
# 同步都会静默少一块数据，所以这里逐项检查并给出该跑的上游命令。

RA_INIT = "在 RecipeAnalysis 运行 `ra inputs init --label {pack}`（校验输入、解包派生 data）"
RA_BUILD = "在 RecipeAnalysis 运行 `ra ingest` 与 `ra run`（产出 facts/ 与 graph.json）"
WGS_COLLECT = "先在 WorldGenStatsCollector 跑一次世界统计采集（产出 stats/<run>/）"


@dataclass
class Prerequisite:
    """一项上游前置条件。缺了它同步出来的 `data/` 就是残缺的。"""

    key: str
    path: Path
    action: str

    @property
    def present(self) -> bool:
        return self.path.exists()

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "path": str(self.path),
            "present": self.present,
            "action": "" if self.present else self.action,
        }


def preflight(pack: str) -> list[Prerequisite]:
    """同步前检查上游是否已经解包/构建过。返回逐项结果，缺的项带补救动作。"""
    ra = paths.REF_RECIPE_ANALYSIS
    init = RA_INIT.format(pack=pack)
    return [
        Prerequisite("ra_inputs", ra / "inputs" / pack / "data", init),
        # 结构模板不在 inputs/ 里：它是 datagen 从 jar 转出的 .snbt 缓存，目录名带 hash。
        Prerequisite("ra_structures", paths.upstream_vanilla_structures(pack).path, init),
        Prerequisite("ra_output", ra / "output" / pack, RA_BUILD),
        # 合并后的数据包：地形包 overlay 的覆盖结果在这里，不是原版解包目录。
        Prerequisite("ra_datapack_merged", paths.upstream_datapack_merged(pack).path, RA_BUILD),
        Prerequisite("wgs_run", paths.upstream_collector_run(pack).path, WGS_COLLECT),
    ]


def missing_prerequisites(pack: str) -> list[Prerequisite]:
    """`preflight()` 里缺掉的那些。"""
    return [item for item in preflight(pack) if not item.present]


def _write_manifest(pack: str, report: SyncReport) -> Path:
    path = paths.UPSTREAM_ROOT / pack / MANIFEST_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": MANIFEST_SCHEMA,
        "pack": pack,
        "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "upstream": {
            "recipe_analysis": _git_head(paths.REF_RECIPE_ANALYSIS),
            "worldgen_stats": _git_head(paths.REF_COLLECTOR),
        },
        **report.as_dict(),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


# ------------------------------------------------------------------ 校验


@dataclass
class VerifyResult:
    pack: str
    ok: bool
    problems: list[str] = field(default_factory=list)
    checked: int = 0

    def as_dict(self) -> dict:
        return {
            "pack": self.pack,
            "ok": self.ok,
            "checked": self.checked,
            "problems": self.problems,
        }


def verify(pack: str) -> VerifyResult:
    """按 manifest 校验本地副本是否完整、是否与记录一致。"""
    manifest_path = paths.UPSTREAM_ROOT / pack / MANIFEST_NAME
    if not manifest_path.exists():
        return VerifyResult(
            pack=pack,
            ok=False,
            problems=[f"没有 manifest：{manifest_path}；先跑 `bg data sync`"],
        )
    try:
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return VerifyResult(pack=pack, ok=False, problems=[f"manifest 不是合法 JSON：{exc}"])

    problems: list[str] = []
    schema = doc.get("schema")
    if schema != MANIFEST_SCHEMA:
        # 旧 manifest 的 kind 集合与新版本不同，逐条比对会得出错误结论。
        problems.append(
            f"manifest 格式是 {schema!r}，当前需要 {MANIFEST_SCHEMA}；"
            "跑 `bg data sync` 重新生成"
        )
    checked = 0
    for raw in doc.get("entries") or []:
        entry = SyncEntry.from_dict(raw)
        target = Path(entry.target)
        if not target.exists():
            problems.append(f"{entry.kind}: 目录不存在 {target}")
            continue
        files, total, digest = _tree_digest(target)
        checked += 1
        if files != entry.files:
            problems.append(f"{entry.kind}: 文件数 {files} != 记录 {entry.files}")
        if digest != entry.digest:
            problems.append(f"{entry.kind}: 内容摘要不符（可能被改动或上游已更新）")
        if total != entry.bytes:
            problems.append(f"{entry.kind}: 总字节 {total} != 记录 {entry.bytes}")

    if not doc.get("entries"):
        problems.append("manifest 没有任何条目")

    return VerifyResult(pack=pack, ok=not problems, checked=checked, problems=problems)
