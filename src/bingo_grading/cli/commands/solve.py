"""求解类命令：`grade` / `plan` / `observe` / `mining`。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pathlib import Path
from ...case.context import CaseContext, CaseError
from ...case.registry import builder_for
from ...plan import PlanError, SolveError, SupplyError, TierError
from ...plan import solve as solve_plan
from ...plan.explain import PathTree, rank_paths, render as render_paths
from ...plan.whole import WholeChoice, to_step_tree, whole_choice, whole_choices
from ...plan.frontiers import frontiers as frontier_rows
from ...route_graph_ra import write_outputs as write_route_graph
from ...route_graph_ra.options import RunInfo
from ...mining import MiningError, estimate_from_row
from ... import grade as grade_module
from ... import observe as observe_module
from ...sources import actions as source_actions
from ...sources import actionsets
from ...sources import graph as graph_module
from ...stats import store as stats_store
from ...gamedata import MIN_DENSITY_BLOCKS
from ... import units

from ..common import _score, _tier, index_path, print_json, stats_path
from ..grade_table import _emit_histogram, _grade_all, _grade_items, _grade_one, _grade_solved, _grade_together, _graph_file

# 画图时要「列到底」才能知道总共有几条路线（实测最深的物品 86 条、约 0.25 s）。
# 上限只是防某个物品的顶层来源爆掉，正常用不到。
PATH_LIST_CAP = 200




def cmd_mining(args: argparse.Namespace) -> int:
    """按群系求「挖到某方块」的 TU 代价。"""
    store = stats_store.load(stats_path(args.pack_version, args.stats))

    rows = [
        row
        for row in store.rows.values()
        if row.stat_type in ("ore_density", "block_density")
        and row.subject == args.subject
        and row.dimension == args.dimension
        and (not args.biome or row.biome == args.biome)
    ]
    if not rows:
        print_json(
            {
                "subject": args.subject,
                "dimension": args.dimension,
                "biome": args.biome or "*",
                "note": "没有该方块的群系级密度行；先跑 bg data ingest",
                "candidates": [],
            }
        )
        return 1

    candidates = []
    for row in sorted(rows, key=lambda item: item.biome):
        try:
            estimate = estimate_from_row(
                row,
                target_items=args.items,
                surface_y=args.surface_y,
                break_block=args.break_block,
                entries_per_block=args.entries_per_block,
            )
        except MiningError as exc:
            candidates.append({"biome": row.biome, "error": str(exc)})
            continue
        payload = estimate.as_dict()
        payload["tier"] = None if estimate.cost_tu is None else _tier(estimate.cost_tu)
        payload["score"] = None if estimate.cost_tu is None else _score(estimate.cost_tu)
        if not args.expand:
            # 默认只给结论：最优段 + 假设。全部纵段要 --expand。
            payload.pop("all_sections", None)
            if payload.get("best_section"):
                payload["best_section"].pop("volume_density", None)
        candidates.append(payload)

    usable = [item for item in candidates if item.get("cost_tu") is not None]
    # 与 DAG 案例同一规则：优先在样本够的群系里取最优，全都不够才退回全部。
    trusted = [item for item in usable if item.get("density_trusted")]
    pool = trusted or usable
    best = min(pool, key=lambda item: item["cost_tu"]) if pool else None

    output: dict = {
        "subject": args.subject,
        "dimension": args.dimension,
        "target_items": args.items,
        "best": best,
        "candidate_count": len(candidates),
        "trusted_count": len(trusted),
        "note": (
            "代价 = 到达该层 + 挖矿道；--expand 看全部纵段。"
            f"样本量判据为方块数 >= {MIN_DENSITY_BLOCKS}（见 ADR 0019）"
        ),
    }
    if args.expand:
        output["candidates"] = candidates
    else:
        output["candidates"] = [
            {
                key: item[key]
                for key in ("biome", "cost_tu", "tier", "score", "density_trusted", "sample_blocks")
                if key in item
            }
            for item in candidates
        ]
    print_json(output)
    return 0 if usable else 1



def _input_digest(args: argparse.Namespace) -> str:
    """输入指纹（来源索引 + 统计快照 + 配方/知识表），失败时返回空串。

    它只用于「两份图是不是同一批数据画的」这一判断，算不出来不该让画图失败。
    """
    try:
        index_path = (
            Path(args.index)
            if getattr(args, "index", None)
            else source_actions.default_index(args.pack_version)
        )
        return actionsets.digest_inputs(
            pack=args.pack_version,
            index_path=index_path,
            stats_path=stats_path(args.pack_version, getattr(args, "stats", None)),
        )
    except Exception:  # noqa: BLE001 - 指纹是附加信息，不阻断主流程
        return ""


def _display_path(value: str) -> str:
    """路径只显示「从哪里来」，不显示本机绝对路径。

    绝对路径带用户名，不该进产物（与 `actionsets._relative` 同一条约束）。
    仓库内的相对路径原样保留（它能说明用了哪份输入），绝对路径退成文件名。
    """
    path = Path(value)
    if path.is_absolute():
        return path.name
    return value


def _run_info(
    args: argparse.Namespace, actionset, *, output_hint: str = "PATH", digest: str = ""
) -> RunInfo:
    """还原本图的生成参数：一条可复跑的命令 + 逐项明细。

    **不含 `--graph-out`**：它只是产物写到哪里，与图长什么样无关，而读者拿到
    HTML 时文件已经在那儿了。命令里用占位符 `PATH` 提示需要自己填，不填
    `<路径>` 这类尖括号——在 bash / PowerShell 里会被当成重定向。

    `--stats` / `--index` 影响内容，保留；值只留相对路径或文件名。
    """
    entries: list[tuple[str, str]] = [
        ("--item", str(args.item)),
        ("--count", f"{args.count:g}"),
        ("--pack-version", str(args.pack_version)),
        ("--graph-paths", str(max(1, int(getattr(args, "graph_paths", 3))))),
        ("--graph-depth", str(max(1, int(getattr(args, "graph_depth", 16))))),
        ("--graph-frontiers", str(max(0, int(getattr(args, "graph_frontiers", 8))))),
        ("--graph-branches", str(max(0, int(getattr(args, "graph_branches", 3))))),
        ("--graph-max-nodes", str(max(0, int(getattr(args, "graph_max_nodes", 500))))),
        (
            "--graph-branch-ratio",
            f"{float(getattr(args, 'graph_branch_ratio', 1.5) or 0.0):g}",
        ),
    ]
    dimension = str(getattr(args, "dimension", "minecraft:overworld"))
    if dimension != "minecraft:overworld":
        entries.append(("--dimension", dimension))
    for name in ("stats", "index"):
        value = getattr(args, name, None)
        if value:
            entries.append((f"--{name}", _display_path(str(value))))
    switches: list[str] = []
    if getattr(args, "no_prune", False):
        switches.append("--no-prune")
    command = " ".join(
        [
            "bg plan",
            *(f"{flag} {value}" for flag, value in entries),
            *switches,
            f"--graph-out {output_hint}",
        ]
    )
    entries.extend((flag, "（开关）") for flag in switches)
    return RunInfo(
        command=command,
        entries=tuple(entries),
        action_count=len(getattr(actionset, "actions", ()) or ()),
        stats_digest=digest,
    )


def _print_plan_summary(
    solved,
    built,
    *,
    graph: list[str] | None,
    out_path: Path | None,
    whole=None,
) -> None:
    """`plan` 的终端摘要：结论 + 动作链摘要 + 产物路径。

    完整诊断（曲线、假设、剪枝、来源、缺口）体量在百 KB 级，默认只写盘。
    路线与分数按**整次调用**口径给（`whole`），均摊值只作参考：
    连续松弛允许「调用 0.54 次」，那不是能执行的次数。
    """
    plan = solved.plan
    demand = f"{plan.demand:g} 个"
    if whole is not None and whole.cost is not None:
        head = (
            f"{plan.target}  {demand} → {whole.cost:.2f} TU"
            f"（{units.score_from_cost(whole.cost)} 分 / 第 {units.tier_of(whole.cost)} 档）"
        )
        note = (
            f"  口径  整次调用（不可分割动作按整次计）；"
            f"均摊参考 {plan.cost:.2f} TU"
        )
        route = f"  路线  {whole.action_id or '（无动作）'} ×{whole.calls:g}"
    elif plan.feasible:
        head = (
            f"{plan.target}  {demand} → {plan.cost:.2f} TU"
            f"（{plan.score} 分 / 第 {plan.tier} 档）"
        )
        note = (
            f"  分项  local {plan.local_cost:.2f} + per_use {plan.per_use_cost:.2f}"
            f" + once {plan.once_cost:.2f}（共享省 {plan.shared_saving:.2f}）"
        )
        route = ""
    else:
        head = f"{plan.target}  {demand} → 不可行：{plan.missing or '未知原因'}"
        note = ""
        route = ""
    print(head)
    if note:
        print(note)
    if route:
        print(route)
    steps = plan.steps
    if steps and not (whole is not None and whole.cost is not None):
        shown = "、".join(
            f"{step.action_id} ×{step.invocations:g}" for step in steps[:4]
        )
        tail = f" 等 {len(steps)} 个动作" if len(steps) > 4 else ""
        print(f"  动作  {shown}{tail}")
    if graph:
        print(f"  图    {'、'.join(graph)}")
    diag = [
        f"剪枝 {len(solved.pruned)}",
        f"假设 {len(built.assumptions)}",
        f"来源 {len(built.provenance)}",
        f"缺口 {built.extras.get('uncovered_total', 0)}",
    ]
    if out_path is not None:
        print(f"  诊断  {out_path}（{'，'.join(diag)}）")
    else:
        print(f"  诊断  {'，'.join(diag)}；加 --out PATH 存完整 JSON，--json 打到终端")


def cmd_plan(args: argparse.Namespace) -> int:
    """按供给曲线算某件物品的获得路线（含前置分层与竞争）。

    自 2026-11-12 起，任何物品都能算：动作图从来源索引自动拼，
    不再要求先在 `case/registry.py` 登记。`--index` 可指向另一份来源索引。
    """
    builder = builder_for(
        args.item,
        pack=args.pack_version,
        index_path=Path(args.index) if getattr(args, "index", None) else None,
    )

    ctx = CaseContext.load(
        stats_path(args.pack_version, args.stats), dimension=args.dimension
    )
    try:
        built = builder(ctx)
        actionset = built.actionset()
        solved = solve_plan(
            actionset,
            args.item,
            args.count,
            prune_dominated=not getattr(args, "no_prune", False),
        )
    except (CaseError, PlanError, SupplyError, TierError, SolveError) as exc:
        print_json({"item": args.item, "error": str(exc)})
        return 1

    payload = solved.as_dict(detail=args.expand)
    payload["assumptions"] = built.assumptions
    # 路线与成本按**整次调用**口径重算：连续松弛允许「调用 0.54 次」，那是批量
    # 规划用的；单件规划里「跑一趟结构」要么进要么不进。与 `bg grade` 共用
    # `plan/whole.py` 的递归，两处不会对同一件事给出两个答案。
    #
    # 要画图时**列到底**（实测 `diamond` 26 条 / `iron_ingot` 86 条，各约 0.25 s）：
    # 只有知道总数，才能在图和表上写「还有几条没画」——`--graph-paths` 默认只画 3 条。
    graph_paths = max(1, int(getattr(args, "graph_paths", 3)))
    path_limit = max(1, int(getattr(args, "path_limit", 3)))
    want_all = bool(getattr(args, "graph_out", None))
    choice_list = whole_choices(
        actionset,
        solved.curves,
        args.item,
        args.count,
        limit=PATH_LIST_CAP if want_all else max(graph_paths, path_limit),
    )
    choice = (
        choice_list[0]
        if choice_list
        else whole_choice(
            actionset, solved.curves, args.item, args.count, want_children=True
        )
    )
    whole_ok = choice.cost is not None
    if whole_ok:
        # `cost_tu` 与 `bg grade` 同名同义：**均摊口径**（连续松弛）。整次成本只放
        # `whole_cost_tu`。两处同名不同义时，把两份 JSON 一起读的脚本会静默算错
        # （实测 `diamond` 的 `cost_tu` 曾是 91.85 与 78.08 两个值）。
        # `tier` / `score` 仍按整次算——单件规划问的是「真跑一次要多久」。
        amortised = (payload.get("plan") or {}).get("cost_tu")
        if amortised is not None:
            payload["cost_tu"] = amortised
        payload["whole_cost_tu"] = round(choice.cost, 4)
        payload["tier"] = units.tier_of(choice.cost)
        payload["score"] = units.score_from_cost(choice.cost)
        payload["feasible"] = True
        payload.pop("missing", None)

    def whole_path_tree(name: str, item_choice: WholeChoice, index: int, best: float) -> PathTree:
        cost = item_choice.cost or 0.0
        return PathTree(
            item=name,
            rank=index + 1,
            unit_cost=(cost / args.count if args.count else cost),
            rank_gap=cost - best,
            root=to_step_tree(
                item_choice,
                depth=max(1, int(getattr(args, "graph_depth", 16))),
            ),
            total_cost=cost,
            need=args.count,
        )
    wanted = list(args.paths_item or [])
    if args.paths:
        wanted.insert(0, args.item)
    if wanted:
        trees = []
        blank = []
        for name in wanted:
            extra = (
                choice_list
                if name == args.item
                else whole_choices(
                    actionset, solved.curves, name, args.count, limit=args.path_limit
                )
            )
            if extra:
                trees.extend(
                    whole_path_tree(
                        name,
                        item,
                        index,
                        extra[0].cost or 0.0,
                    )
                    for index, item in enumerate(
                        extra[: max(1, int(args.path_limit))]
                    )
                )
                continue
            found = rank_paths(actionset, solved, name, limit=args.path_limit)
            if not found:
                blank.append(name)
            trees.extend(found)
        payload["paths"] = [tree.as_dict() for tree in trees]
        payload["paths_text"] = render_paths(trees)
        if blank:
            payload["paths_missing"] = blank
    if getattr(args, "graph_out", None):
        if whole_ok:
            # 整次口径：前 N 条 = 逐个排除上一条的顶层来源，前置各取自己的整次最优。
            drawn = choice_list[:graph_paths]
            trees = [
                whole_path_tree(args.item, item, index, choice_list[0].cost or 0.0)
                for index, item in enumerate(drawn)
            ]
            best_cost = choice_list[0].cost or 0.0
            # 没画出来的路线：`--graph-paths` 截掉的那些，图上要留痕。
            skipped_paths = tuple(
                (
                    index + 1 + len(drawn),
                    (item.cost or 0.0) / best_cost if best_cost > 0 else 1.0,
                    item.cost or 0.0,
                    item.action_id or "",
                )
                for index, item in enumerate(choice_list[len(drawn) :])
            )
        else:
            trees = rank_paths(
                actionset,
                solved,
                args.item,
                limit=graph_paths,
                depth=max(1, int(getattr(args, "graph_depth", 16))),
            )
            skipped_paths = ()
        if trees:
            rows = None
            limit = max(0, int(getattr(args, "graph_frontiers", 8)))
            # 整次口径的树不能与均摊曲线比差额（`frontiers` 会重新 `solve`），先不画。
            if limit and not whole_ok:
                rows = [
                    row.as_dict()
                    for row in frontier_rows(
                        actionset,
                        solved,
                        trees[0],
                        limit=limit,
                        prune_dominated=not getattr(args, "no_prune", False),
                    )
                ]
            written = write_route_graph(
                Path(args.graph_out),
                trees,
                args.item,
                frontiers=rows,
                run=_run_info(
                    args,
                    actionset,
                    digest=_input_digest(args),
                ),
                solved=solved,
                actionset=actionset,
                branches=max(0, int(getattr(args, "graph_branches", 3))),
                max_nodes=max(0, int(getattr(args, "graph_max_nodes", 500))),
                ratio_limit=float(getattr(args, "graph_branch_ratio", 1.5) or 0.0),
                skipped_paths=skipped_paths,
            )
            payload["graph"] = {
                "out": written,
                "paths": len(trees),
                "paths_skipped": len(skipped_paths),
            }
            if whole_ok and max(0, int(getattr(args, "graph_frontiers", 8))):
                # 前置替换成本要重新 `solve` 并按均摊曲线比差额，与整次树不同口径；
                # 明说没算，而不是让读者以为「没有脆弱前置」。
                payload["graph"]["frontiers_skipped"] = (
                    "整次口径下暂不计算（需要按整次重解整条路线）"
                )
        else:
            payload["graph"] = {"out": [], "error": "没有可画的路线"}
    payload["provenance"] = {
        name: sourced.as_dict() for name, sourced in built.provenance.items()
    }
    frames = built.extras.get("frames") or ()
    if args.expand and frames:
        payload["frames"] = [frame.as_dict() for frame in frames]
    # 未接入来源跟着一起输出：没接上的东西必须是可见的。
    for key in (
        "uncovered",
        "uncovered_total",
        "source_records",
        "source_by_type",
        "gaps",
    ):
        if key in built.extras:
            payload[key] = built.extras[key]
    out_path = Path(args.out) if getattr(args, "out", None) else None
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8",
        )
    if getattr(args, "json", False):
        print_json(payload)
    else:
        _print_plan_summary(
            solved,
            built,
            graph=(payload.get("graph") or {}).get("out") or None,
            out_path=out_path,
            whole=choice if whole_ok else None,
        )
        if payload.get("paths_text"):
            print()
            print(payload["paths_text"])
    return 0 if (whole_ok or solved.plan.feasible) else 1



def cmd_grade(args: argparse.Namespace) -> int:
    """一批物品的 Bingo 难度表。

    与 `plan` 的区别在输入形态：那个一次回答一件，
    这个回答「一张卡整体有多难，哪几件的分数不可信」。
    评分口径仍走 `bingo_grading.units.score_from_cost`，这里不另立一套。
    """
    ctx = CaseContext.load(
        stats_path(args.pack_version, args.stats), dimension=args.dimension
    )
    if getattr(args, "all", False):
        return _grade_all(ctx, args)
    items = _grade_items(args)
    if not items:
        print_json({"error": "没有目标物品：用 --item / --file 给一份清单，或 --all 算全库"})
        return 1
    if getattr(args, "together", False):
        return _grade_together(ctx, items, args)
    index_path = Path(args.index) if args.index else source_actions.default_index(
        args.pack_version
    )
    try:
        graph = _graph_file(ctx, args, index_path)
    except actionsets.ActionsFileError as exc:
        print_json({"error": str(exc)})
        return 1
    if len(items) > grade_module.JOINT_THRESHOLD:
        rows = _grade_solved(graph, items, args)
    else:
        rows = [_grade_one(graph, item, args) for item in items]
    payload = {
        "pack": args.pack_version,
        "count": args.count,
        "items": [row.as_dict() for row in rows],
        "summary": grade_module.summarize(rows),
        "table": grade_module.render_table(rows),
    }
    if args.out:
        # 中文一律由脚本自己按 UTF-8 写，不走终端重定向。
        Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8",
        )
        payload["out"] = str(args.out)
    if getattr(args, "table_out", None):
        payload["table_out"] = [
            {"path": str(path), "format": grade_module.render_table_file(rows, path)}
            for path in args.table_out
        ]
    if getattr(args, "histogram", False) or getattr(args, "histogram_out", None):
        if len(items) > grade_module.JOINT_THRESHOLD:
            solved = solve_plan(
                graph.actionset, items[0], args.count, prune_dominated=False
            )
        else:
            solved = None
        payload["histogram"] = _emit_histogram(rows, graph.actionset, solved, args)
    if args.table:
        print(payload["table"])
    else:
        print_json(payload)
    return 0



def cmd_observe(args: argparse.Namespace) -> int:
    """摊开若干节点：最便宜段、前置花费、备选来源、另一套求解器的对照。"""
    ctx = CaseContext.load(
        stats_path(args.pack_version, args.stats), dimension=args.dimension
    )
    index_path = Path(args.index) if args.index else source_actions.default_index(
        args.pack_version
    )
    try:
        graph = _graph_file(ctx, args, index_path)
    except actionsets.ActionsFileError as exc:
        print_json({"error": str(exc)})
        return 1
    items = _grade_items(args)
    if not items:
        print_json({"error": "没有目标节点：用 --item 给一份清单"})
        return 1
    by_id = {action.action_id for action in graph.actionset.actions}
    wanted_actions = [name for name in items if name in by_id]
    wanted_items = [name for name in items if name not in by_id]
    if not wanted_items and not wanted_actions:
        print_json({"error": "清单里的名字既不是物品也不是动作"})
        return 1
    target = (
        wanted_items[0]
        if wanted_items
        else (graph.targets[0] if graph.targets else "minecraft:diamond")
    )

    try:
        solved = solve_plan(graph.actionset, target, args.count, prune_dominated=False)
    except (PlanError, SupplyError, TierError, SolveError) as exc:
        print_json({"error": str(exc)})
        return 1
    prices = observe_module.price_table(solved, args.count)
    nodes = [
        observe_module.report_node(
            graph.actionset,
            solved,
            item,
            demand=args.count,
            neighbours_of=observe_module.neighbours(prices, item, args.nearby),
        )
        for item in wanted_items
    ]
    actions = [
        observe_module.report_action(graph.actionset, solved, action_id)
        for action_id in wanted_actions
    ]
    sections = []
    if nodes:
        sections.append(observe_module.render(nodes))
    if actions:
        sections.append(observe_module.render_actions(actions))
    payload = {
        "pack": args.pack_version,
        "count": args.count,
        "solver": "dag",
        "budget": graph.budget,
        "nodes": nodes,
        "actions": actions,
        "table": "\n\n".join(sections),
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
