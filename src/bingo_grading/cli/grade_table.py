"""难度表：联合构建、逐件取价、直方图。`cmd_grade` 的私有实现都在这里。"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from pathlib import Path
from ..case.context import CaseContext, CaseError
from ..plan import PlanError, SolveError, SupplyError, TierError
from ..plan import solve as solve_plan
from .. import grade as grade_module
from .. import histogram as histogram_module
from ..sources import actions as source_actions
from ..sources import actionsets
from ..sources import graph as graph_module
from ..sources import index as source_index

from .common import _close, _progress, _write_console, index_path, print_json, stats_path




def _grade_items(args: argparse.Namespace) -> list[str]:
    """从 `--item`（可重复、可逗号分隔）与 `--file` 收集物品名，保序去重。"""
    names: list[str] = []
    for chunk in args.item:
        names.extend(part.strip() for part in chunk.split(",") if part.strip())
    source = getattr(args, "file", None)
    if source:
        path = Path(source)
        if not path.exists():
            raise CaseError(f"物品清单不存在：{path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            # 一行一个物品；`#` 之后是注释，方便在清单里写备注。
            entry = line.split("#", 1)[0].strip()
            if entry:
                names.append(entry)
    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        # 索引里的物品名都带命名空间；清单里写 `diamond` 也认。
        full = name if ":" in name else f"minecraft:{name}"
        if full not in seen:
            seen.add(full)
            unique.append(full)
    return unique



def _whole_items(index_path: Path) -> tuple[dict, list[str], dict[str, int]]:
    """来源索引里的全部物品，以及每件的来源条数。"""
    header, records = source_index.read(index_path)
    counts: dict[str, int] = {}
    for record in records:
        counts[record.item] = counts.get(record.item, 0) + 1
    return header, sorted(counts), counts



def _build_whole_graph(
    ctx: CaseContext, args: argparse.Namespace, index_path: Path
):
    """一次构建覆盖索引里的全部物品。"""
    header, items, counts = _whole_items(index_path)
    if not items:
        raise CaseError(f"来源索引里没有任何物品：{index_path}")
    print(
        f"来源索引 {header.get('label')}：{len(items)} 个物品、"
        f"{sum(counts.values())} 条来源",
        file=sys.stderr,
    )
    bar = _progress(len(items), "建图")

    def on_visit(index: int, total: int, name: str) -> None:
        if bar is not None:
            bar.set_postfix_str(name.split(":", 1)[-1][:28], refresh=False)
            bar.update(1)

    built = source_actions.build(
        ctx,
        items[0],
        pack=args.pack_version,
        index_path=index_path,
        extra_targets=items[1:],
        progress=on_visit,
    )
    _close(bar)
    return built, items, counts



def _build_and_write(
    ctx: CaseContext,
    args: argparse.Namespace,
    index_path: Path,
    path: Path,
    digest: str,
) -> dict:
    built, items, counts = _build_whole_graph(ctx, args, index_path)
    actionset = built.actionset()
    bar = _progress(1, "求解全库图")
    # **不做 Pareto 剪枝**：剪枝是全局的，而最优是 per-target 的。
    solved = solve_plan(actionset, items[0], 1.0, prune_dominated=False)
    _close(bar)
    return actionsets.write(
        path,
        actionset,
        pack=args.pack_version,
        budget=solved.budget,
        sources=counts,
        uncovered=dict(built.uncovered),
        stats_digest=digest,
        no_action_items=built.no_action_items,
        missing_prereqs=dict(built.missing_prereqs),
    )



def _graph_file(
    ctx: CaseContext, args: argparse.Namespace, index_path: Path
) -> actionsets.GraphFile:
    """读落盘的全图；`--rebuild` 时先重建。"""
    path = Path(args.actions) if getattr(args, "actions", None) else (
        actionsets.default_path(args.pack_version)
    )
    digest = actionsets.digest_inputs(
        pack=args.pack_version,
        index_path=index_path,
        stats_path=stats_path(args.pack_version, args.stats),
    )
    if getattr(args, "rebuild", False):
        _build_and_write(ctx, args, index_path, path, digest)
    return actionsets.read(path, expect_digest=digest)



def _rows_from_curves(
    graph: actionsets.GraphFile,
    actionset,
    solved,
    items: list[str],
    args: argparse.Namespace,
    *,
    bar=None,
):
    """在一份已求解的曲线上逐件取整次成本。单件与全库共用这一处。"""
    uncovered = graph.uncovered
    counts = graph.sources
    # 只共享 `action_id` 索引（几千条，逐件重建纯属浪费）。
    # **不共享 `memo`**：`best_whole_cost` 的 `seen` 是防环用的，某个物品在
    # 宽松 `seen` 下算出的价会被严格链路的物品复用，环上的物品因此偏悲观——
    # 实测 `waxed_copper_door` 单件 9.79、共享 memo 后 301.35。
    by_id = {action.action_id: action for action in actionset.actions}
    rows = []
    for name in items:
        whole, action_id, label, spread = grade_module.best_whole_cost(
            actionset,
            solved.curves,
            name,
            args.count,
            actions_by_id=by_id,
        )
        curve = solved.curve_of(name)
        try:
            segment = curve.segment_at(args.count)
        except SupplyError:
            segment = None
        try:
            amortised = curve.cost(args.count)
        except SupplyError:
            amortised = math.inf
        rows.append(
            grade_module.row_of(
                item=name,
                cost=amortised if math.isfinite(amortised) else None,
                whole_cost=whole,
                feasible=whole is not None,
                route=action_id or ("" if segment is None else segment.action_id),
                route_label=label or ("" if segment is None else segment.label),
                sources=counts.get(name, 0),
                uncovered=uncovered,
                segments=len(curve.segments),
                demand=args.count,
                variance=spread,
            )
        )
        if bar is not None:
            bar.set_postfix_str(name.split(":", 1)[-1][:28], refresh=False)
            bar.update(1)
    return rows



def _grade_solved(
    graph: actionsets.GraphFile, items: list[str], args: argparse.Namespace
) -> list:
    """多件清单：整图求一次曲线，再逐件取价。

    子图求解每件约 1.5 秒，整图一次约 6 秒；清单超过
    `grade_module.JOINT_THRESHOLD` 件时整图更省。两条路径数值相同
    （见 `test_actionsets`）。
    """
    bar = _progress(1, "求解")
    solved = solve_plan(
        graph.actionset,
        items[0],
        args.count,
        prune_dominated=False,
    )
    _close(bar)
    bar = _progress(len(items), "取整次成本")
    rows = _rows_from_curves(graph, graph.actionset, solved, items, args, bar=bar)
    _close(bar)
    return rows



def _grade_one(graph: actionsets.GraphFile, item: str, args: argparse.Namespace):
    """在落盘的全图里取一件物品的难度。单件失败不影响整张表。

    求解用目标自己的子图（反向可达），`budget` 用落盘值：全图 4481 条动作
    一次求解约 5 秒，单件查询不该付这个代价；而层数固定、无关动作对目标
    曲线没有贡献，裁掉不改变结果。
    """
    subset = graph.subgraph(item)
    if not subset.actions:
        return grade_module.row_of(
            item=item,
            cost=None,
            whole_cost=None,
            feasible=False,
            route="",
            route_label="动作图里没有这件物品（它不在来源索引里）；加 `--rebuild` 重建全图",
            sources=graph.sources.get(item, 0),
            uncovered=graph.uncovered,
            segments=0,
        )
    try:
        solved = solve_plan(subset, item, args.count, prune_dominated=False)
    except (PlanError, SupplyError, TierError, SolveError) as exc:
        return grade_module.row_of(
            item=item,
            cost=None,
            whole_cost=None,
            feasible=False,
            route="",
            route_label=f"求解失败：{exc}",
            sources=graph.sources.get(item, 0),
            uncovered=graph.uncovered,
            segments=0,
        )
    curve = solved.curve_of(item)
    feasible = solved.plan.feasible
    if not feasible:
        return grade_module.row_of(
            item=item,
            cost=None,
            feasible=False,
            route="",
            route_label="求不出可行路线",
            sources=graph.sources.get(item, 0),
            uncovered=graph.uncovered,
            segments=len(curve.segments),
            demand=args.count,
        )
    return _rows_from_curves(graph, subset, solved, [item], args)[0]



def _grade_together(
    ctx: CaseContext, items: list[str], args: argparse.Namespace
) -> int:
    """把整份清单当成**一个组合**求一次买齐的成本。

    与逐件算完再相加的区别：`loot:buried_treasures` 一趟同时给钻石 0.96、
    金锭 4.48、铁锭 8.95，所以「钻石 + 金锭 + 铁锭」跑两趟就够，
    而不是 11.38 + 5.69 + 0.72。共用前置（村庄、末地门）也只收一次。
    """
    built = None
    for item in items:
        try:
            one = source_actions.build(
                ctx,
                item,
                pack=args.pack_version,
                index_path=Path(args.index) if args.index else None,
            )
        except (CaseError, PlanError, SupplyError, TierError) as exc:
            print_json({"error": f"{item} 拼不出动作图：{exc}"})
            return 1
        if built is None:
            built = one
            continue
        # 组合图要让每件物品的来源都在图里：把后一件的动作并进来。
        for action in one.bundle.actions:
            built.emit(action)
        for key, count in one.uncovered.items():
            built.uncovered[key] += count
    assert built is not None
    aset = built.actionset()
    demands = {item: args.count for item in items}
    # `curves` 必须是真曲线：`best_bundle` 的前置定价走 `plan/whole.py`，
    # 没有曲线就无从递归（此前这里传 `None`，因为老实现完全不用曲线）。
    bundled = solve_plan(aset, items[0], args.count, prune_dominated=False)
    bundle = grade_module.best_bundle(aset, bundled.curves, demands)
    if bundle.cost is None:
        print_json({"error": "组合算不出可行路线", "demands": demands})
        return 1
    # 对照：逐件各求一次整次成本，加起来
    separate = 0.0
    breakdown: list[dict] = []
    for item in items:
        solved = solve_plan(aset, item, args.count)
        cost, action_id, label, _spread = grade_module.best_whole_cost(
            aset, solved.curves, item, args.count
        )
        breakdown.append(
            {
                "item": item,
                # 逐件对照用的是整次口径（与 `bundle` 同口径，两者的差才有意义）。
                "whole_cost_tu": None if cost is None else round(cost, 4),
                "route": action_id,
                "route_label": label,
            }
        )
        separate += cost or 0.0
    saving = separate - bundle.cost
    lines = [
        f"组合 {len(items)} 件：一次买齐 {bundle.cost:.2f} TU"
        f"（分别买 {separate:.2f}，省 {saving:.2f}）",
        "",
        f"{'动作':52}{'次数':>6}  覆盖",
    ]
    for step in bundle.steps:
        covers = ", ".join(item.split(":")[-1] for item in step.covers)
        lines.append(f"{step.action_id:52}{step.calls:>6g}  {covers}")
    payload = {
        "pack": args.pack_version,
        "count": args.count,
        "together": {
            "demands": demands,
            **bundle.as_dict(),
            "separate_tu": round(separate, 4),
            "saving_tu": round(saving, 4),
        },
        "separate": breakdown,
        "table": "\n".join(lines),
    }
    if args.out:
        Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8",
        )
        payload["out"] = str(args.out)
    if args.table:
        print(payload["table"])
    else:
        print_json(payload)
    return 0



def _emit_histogram(rows, actionset, solved, args: argparse.Namespace) -> dict:
    """代价直方图：终端文本 + 可选 HTML 文件。

    横轴是整次成本的对数箱（`--histogram-per-octave` 控制档位数量），每箱一根柱，
    三个维度自下而上堆叠。下界/末地入口的代价用 `best_whole_cost` 算——
    和表里的「整次成本」同一口径，否则线会落在错误的位置。
    """
    if not rows:
        return {"error": "没有可统计的物品"}
    if solved is None:
        # 小批量清单走的是逐件求解，没有全库曲线；这里补一次（直方图本来就要全局）。
        solved = solve_plan(
            actionset, rows[0].item, args.count, prune_dominated=False
        )
    dimensions = histogram_module.classify(actionset)
    per_octave = int(getattr(args, "histogram_per_octave", 4) or 4)
    bins = histogram_module.build_cost_bins(rows, dimensions, per_octave=per_octave)
    by_id = {action.action_id: action for action in actionset.actions}
    marks = []
    for item, label, color in histogram_module.GATES:
        whole, _action_id, _label, _spread = grade_module.best_whole_cost(
            actionset, solved.curves, item, args.count, actions_by_id=by_id
        )
        if whole is None or not math.isfinite(whole) or whole <= 0:
            continue
        marks.append(histogram_module.Mark(label=label, cost=float(whole), color=color))
    lowest = []
    for dim in (histogram_module.NETHER, histogram_module.END):
        candidates = [
            row
            for row in rows
            if dimensions.get(row.item) == dim and row.whole_cost_tu
        ]
        if candidates:
            cheap = min(candidates, key=lambda row: row.whole_cost_tu)
            lowest.append(
                (
                    histogram_module.DIM_LABEL[dim],
                    cheap.item,
                    float(cheap.whole_cost_tu),
                )
            )
    meta = histogram_module.Meta(
        pack=args.pack_version,
        items=len(rows),
        scored=sum(item.total for item in bins),
        demand=args.count,
        axis="cost",
        per_octave=per_octave,
        skip_empty=True,
        lowest=tuple(lowest),
    )
    if getattr(args, "histogram", False):
        _write_console(histogram_module.render_text(bins, marks, meta))
    written = []
    for raw_path in getattr(args, "histogram_out", None) or []:
        target = Path(raw_path)
        if target.parent and not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
        # HTML 自己声明 charset，不写 BOM（BOM 会让浏览器把 DOCTYPE 前多一个字符）。
        target.write_text(
            histogram_module.render_html(bins, marks, meta), encoding="utf-8"
        )
        written.append(str(raw_path))
    return {
        "axis": "cost",
        "per_octave": per_octave,
        "bins": [
            {
                "low": round(item.low, 4),
                "high": round(item.high, 4),
                "total": item.total,
                **item.counts,
            }
            for item in bins
        ],
        "marks": [{"label": mark.label, "cost": round(mark.cost, 4)} for mark in marks],
        "dimensions": histogram_module.totals(bins),
        "out": written,
    }



def _grade_all(ctx: CaseContext, args: argparse.Namespace) -> int:
    """全库联合计算：读落盘动作图 + 一次求解 + 逐件取整次成本。

    逐件跑 `build()` + `solve()` 是 1414 × (215 + 178) ms ≈ 9 分钟，因为每件的
    动作图本来就含它整条配方链（单件 356 个动作），逐件等于把那批动作反复
    重建。联合跑一次建图 5.4s、一次求解 5.0s（curves 覆盖 1271 个物品），
    剩下只是逐件在已有曲线上取整次成本。

    图落盘在 `build/<pack>/actions.jsonl`：存在且输入指纹一致就复用，
    不存在就构建并写盘，指纹不一致则报错要求重建。
    """
    index_path = Path(args.index) if args.index else source_actions.default_index(
        args.pack_version
    )
    path = Path(args.actions) if getattr(args, "actions", None) else (
        actionsets.default_path(args.pack_version)
    )
    digest = actionsets.digest_inputs(
        pack=args.pack_version,
        index_path=index_path,
        stats_path=stats_path(args.pack_version, args.stats),
    )
    if getattr(args, "rebuild", False):
        _build_and_write(ctx, args, index_path, path, digest)
    try:
        graph = actionsets.read(path, expect_digest=digest)
    except actionsets.ActionsFileMissing:
        _build_and_write(ctx, args, index_path, path, digest)
        graph = actionsets.read(path, expect_digest=digest)
    except actionsets.ActionsFileError as exc:
        print_json({"error": str(exc)})
        return 1

    actionset = graph.actionset
    items = graph.targets
    if not items:
        print_json({"error": f"动作图里没有任何目标物品：{path}"})
        return 1
    print(
        f"动作图 {path}：{len(actionset.actions)} 个动作、budget={graph.budget}",
        file=sys.stderr,
    )
    bar = _progress(1, "求解全库图")
    # **不做 Pareto 剪枝**：剪枝是全局的，而最优是 per-target 的——全库图上
    # 被支配判掉的来源，对某些具体物品可能正是唯一可用那条。实测
    # `glowstone_dust` 因此从 42.93 变贵到 45.57（+6%）。
    # 而且这一层不剪反而更快（3.6s vs 4.4s）：4481 个动作时剪枝自身的
    # O(n²) 比较已经比省下的迭代更贵。
    solved = solve_plan(actionset, items[0], args.count, prune_dominated=False)
    _close(bar)

    counts = graph.sources
    bar = _progress(len(items), "取整次成本")
    rows = _rows_from_curves(graph, actionset, solved, items, args, bar=bar)
    _close(bar)

    payload = {
        "pack": args.pack_version,
        "count": args.count,
        "items": [row.as_dict() for row in rows],
        "summary": grade_module.summarize(rows),
        "table": grade_module.render_table(rows),
        "joint": {
            "items": len(items),
            "records": sum(counts.values()),
            "actions": len(actionset.actions),
            "curves": len(solved.curves),
            "budget": solved.budget,
        },
    }
    if args.out:
        Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8",
        )
    # 1414 行的 JSON 不往终端刷：默认只打摘要，明细走 `--out` / `--table` / `--table-out`。
    brief = {
        key: payload[key] for key in ("pack", "count", "summary", "joint")
    }
    if args.out:
        brief["out"] = str(args.out)
    elif not args.table and not getattr(args, "table_out", None):
        brief["hint"] = "明细用 --out 落盘，或加 --table 打对齐表格、--table-out 写表格文件"
    if getattr(args, "table_out", None):
        brief["table_out"] = [
            {"path": str(path), "format": grade_module.render_table_file(rows, path)}
            for path in args.table_out
        ]
    if getattr(args, "histogram", False) or getattr(args, "histogram_out", None):
        brief["histogram"] = _emit_histogram(rows, actionset, solved, args)
    if args.table:
        print(payload["table"])
    print_json(brief)
    return 0
