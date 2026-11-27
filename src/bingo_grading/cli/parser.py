"""`build_parser()`：命令行结构与参数定义。"""

from __future__ import annotations

import argparse

from ..sources.constructs import NETHER_ACCESS
from .. import paths
from ..recipes.expand import MAX_DEPTH
from .. import grade as grade_module
from ..sources import index as source_index
from ..stats.importers import loot as loot_import

from .commands.data import cmd_loot_build, cmd_stats_import, cmd_upstream_sync, cmd_upstream_verify
from .commands.index import cmd_graph_build, cmd_slots, cmd_sources_build
from .commands.misc import cmd_config, cmd_doctor, cmd_kinds, cmd_stats_resolve
from .commands.solve import cmd_grade, cmd_mining, cmd_observe, cmd_plan




def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="grading",
        description="Bingo 物品固有难度评分",
        epilog=(
            "只读命令在顶层；写 data/ 的进 `data` 组，写 build/ 的进 `index` 组。"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    # ── 只读：求解与查询 ──────────────────────────────────────────────

    grade = sub.add_parser(
        "grade",
        help="一批物品的 Bingo 难度表（分数 + 可信度 + 整卡汇总）",
    )
    grade.add_argument(
        "--item",
        action="append",
        default=[],
        help="目标物品，可重复或逗号分隔（minecraft:diamond,minecraft:iron_ingot）",
    )
    grade.add_argument(
        "--file",
        help="从文件读物品清单：一行一个，# 开头为注释",
    )
    grade.add_argument("--count", type=float, default=1.0, help="每件需要几个")
    grade.add_argument("--pack-version", required=True, help="地形包版本")
    grade.add_argument("--stats")
    grade.add_argument("--index", help="来源索引路径；缺省取 build/<pack>/source_index.jsonl")
    grade.add_argument(
        "--actions",
        help="落盘动作图路径；缺省取 build/<pack>/actions.jsonl",
    )
    grade.add_argument("--dimension", default="minecraft:overworld")
    grade.add_argument("--out", help="把结果（JSON）写到文件")
    grade.add_argument(
        "--table",
        action="store_true",
        help="在终端打印对齐表格（中文按两列宽算）",
    )
    grade.add_argument(
        "--table-out",
        action="append",
        metavar="PATH",
        help=(
            "把表格写到文件，格式按后缀：`.md` → Markdown（多一列「说明」）、"
            "`.csv` → CSV、其它 → 对齐文本表。可重复，一次出多份。"
            "全库 1414 行到终端会被截断，用它。"
        ),
    )
    grade.add_argument(
        "--together",
        action="store_true",
        help=(
            "把 --item 里的物品当成**一个组合**求一次买齐的成本，"
            "而不是逐件算完再相加。同一趟能同时产出的东西只付一次行程。"
        ),
    )
    grade.add_argument(
        "--histogram",
        action="store_true",
        help=(
            "在终端打印代价直方图：一行一个整次成本箱，每个维度一段字符，"
            "下界与末地的入口代价挂在对应箱末尾。"
        ),
    )
    grade.add_argument(
        "--histogram-out",
        action="append",
        metavar="PATH",
        help=(
            "把代价直方图写成自包含 HTML（内嵌 SVG，可悬停看每箱明细），"
            "每箱一根柱、三个维度堆叠，下界/末地入口画成虚线。可重复，一次出多份。"
        ),
    )
    grade.add_argument(
        "--histogram-per-octave",
        type=int,
        default=4,
        metavar="N",
        help=(
            "直方图档位密度（默认 4，即每翻倍 4 箱）。"
            "调大档位更多、柱子更细。"
        ),
    )
    grade.add_argument(
        "--all",
        action="store_true",
        help=(
            "算来源索引里的**全部物品**。走联合计算：一次建全库动作图 + 一次求解，"
            "再逐件取整次成本——比逐件 build+solve 快一个量级。带进度条。"
        ),
    )
    grade.add_argument(
        "--rebuild",
        action="store_true",
        help=(
            "重建全库动作图并写盘，不用落盘的那份。"
            "落盘图缺失或输入指纹过期时也需要它。"
        ),
    )
    grade.set_defaults(handler=cmd_grade)

    plan = sub.add_parser(
        "plan", help="按供给曲线算某物品的获得路线（任意物品，免登记）"
    )
    plan.add_argument("--item", default=NETHER_ACCESS, help="目标物品或能力")
    plan.add_argument("--count", type=float, default=1.0, help="需要几个")
    plan.add_argument("--pack-version", required=True, help="地形包版本")
    plan.add_argument("--stats")
    plan.add_argument(
        "--index", help="来源索引路径；缺省取 build/<pack>/source_index.jsonl"
    )
    plan.add_argument("--dimension", default="minecraft:overworld")
    plan.add_argument(
        "--no-prune",
        action="store_true",
        help="不做 Pareto 剪枝（默认剪掉被支配的来源）",
    )
    plan.add_argument("--expand", action="store_true", help="展开步骤、前置与逐层曲线")
    plan.add_argument(
        "--out",
        metavar="PATH",
        help=(
            "把完整诊断写入 PATH（UTF-8 JSON：曲线、假设、剪枝、来源、缺口）。"
            "终端默认只打摘要"
        ),
    )
    plan.add_argument(
        "--json",
        action="store_true",
        help="把完整诊断打到终端（机器可读；想看人话摘要不加这个）",
    )
    plan.add_argument(
        "--paths",
        action="store_true",
        help="列出目标物品的最优/次优完整链（含每一层的原料与单价）",
    )
    plan.add_argument(
        "--paths-item",
        action="append",
        default=[],
        metavar="物品",
        help=(
            "额外列出这些物品的最优/次优链（同一个动作集内，"
            "可重复；用来查铁镐、木棍这类中间物）"
        ),
    )
    plan.add_argument("--path-limit", type=int, default=3, help="列出前几条路线")
    plan.add_argument(
        "--graph-out",
        metavar="PATH",
        help=(
            "把路线画成合成图：`PATH` 是基础名，写 `.dot`，"
            "有 Graphviz 时再写 `.svg` 与 `.html`。颜色按与最优路线的成本比浓淡。"
        ),
    )
    plan.add_argument(
        "--graph-paths",
        type=int,
        default=3,
        metavar="N",
        help=(
            "合成图画前几条路线（默认 3）。没画出来的会在图上标一个「还有 N 条路线」"
            "节点（tooltip 列明细），HTML 的路线对照表下也会列出来"
        ),
    )
    plan.add_argument(
        "--graph-depth",
        type=int,
        default=16,
        metavar="N",
        help=(
            "合成图向下展开几层前置（默认 16，实际会被树的深度封顶）。"
            "超出这一层的节点画成虚线框：它自己的成本已经计入，只是不再展开更下游的明细"
        ),
    )
    plan.add_argument(
        "--graph-frontiers",
        type=int,
        default=8,
        metavar="N",
        help=(
            "合成图附「前置替换成本」表：禁用某前置当前的来源后重算整条路线，"
            "列出差额最大的前 N 处（默认 8，0 关闭）"
        ),
    )
    plan.add_argument(
        "--graph-branches",
        type=int,
        default=3,
        metavar="N",
        help=(
            "每个物品最多画几条候选来源（默认 3 = 最优 + 2 个备选）。"
            "0 = 全部（大物品会到几百个节点、渲染十几秒）；1 = 只画最优"
        ),
    )
    plan.add_argument(
        "--graph-max-nodes",
        type=int,
        default=500,
        metavar="N",
        help=(
            "节点总数上限（默认 500，0 = 不限）。超出的候选不画，"
            "画成橙色虚线的「还有 N 条候选」节点"
        ),
    )
    plan.add_argument(
        "--graph-branch-ratio",
        type=float,
        default=1.5,
        metavar="R",
        help=(
            "候选来源只在「贵不到 R 倍」时才画（默认 1.5）。"
            "差几倍的来源画上去只是噪音，还会把图撑到几千像素宽；0 = 不限"
        ),
    )
    plan.set_defaults(handler=cmd_plan)

    observe_cmd = sub.add_parser(
        "observe",
        help="摊开若干节点：价格来源、前置花费、备选来源、两套求解器对照",
    )
    observe_cmd.add_argument(
        "--item",
        action="append",
        default=[],
        help=(
            "目标节点，可重复或逗号分隔。既可以是物品名，也可以是动作 id"
            "（`portal:full_frame` 这类）——后者会报动作自身的代价与前置。"
            "也认 `capability:nether_access` 这类假物品。"
        ),
    )
    observe_cmd.add_argument("--count", type=float, default=1.0, help="每件需要几个")
    observe_cmd.add_argument("--pack-version", required=True, help="地形包版本")
    observe_cmd.add_argument("--stats")
    observe_cmd.add_argument("--index")
    observe_cmd.add_argument(
        "--actions", help="落盘动作图路径；缺省取 build/<pack>/actions.jsonl"
    )
    observe_cmd.add_argument("--dimension", default="minecraft:overworld")
    observe_cmd.add_argument("--out", help="把结果（JSON）写到文件")
    observe_cmd.add_argument(
        "--nearby",
        type=int,
        default=5,
        help="每个节点报几件邻近价带的物品（0 关闭）",
    )
    observe_cmd.add_argument("--table", action="store_true", help="在终端打印对齐表格")
    observe_cmd.set_defaults(handler=cmd_observe)

    stat = sub.add_parser("stat", help="解析一条统计查询")
    stat.add_argument("--pack", required=True, help="地形包版本")
    stat.add_argument("--stats")
    stat.add_argument("--stat", required=True, help="空间测量类型（命名事实没有层级回退）")
    stat.add_argument("--dimension", default="")
    stat.add_argument("--biome", default="")
    stat.add_argument("--subject", default="")
    stat.add_argument("--condition", default="")
    stat.set_defaults(handler=cmd_stats_resolve)

    mining = sub.add_parser("mining", help="挖到某方块的 TU 代价（按群系）")
    mining.add_argument("--subject", required=True, help="方块 ID，如 minecraft:gold_ore")
    mining.add_argument("--pack-version", required=True, help="地形包版本")
    mining.add_argument("--stats")
    mining.add_argument("--dimension", default="minecraft:overworld")
    mining.add_argument("--biome", help="限定群系；缺省给全部候选")
    mining.add_argument("--items", type=float, default=1.0, help="需要几个物品")
    mining.add_argument(
        "--entries-per-block",
        type=float,
        default=1.0,
        help="每块方块的掉落量（红石矿约 4.5）",
    )
    mining.add_argument("--surface-y", type=int, default=70, help="地表高度")
    mining.add_argument(
        "--break-block",
        help="围岩类型（stone/deepslate）；缺省按层位自动选",
    )
    mining.add_argument("--expand", action="store_true", help="展开全部纵段明细")
    mining.set_defaults(handler=cmd_mining)

    doctor = sub.add_parser("doctor", help="检查上游产物、统计快照与知识表")
    doctor.add_argument("--label", help="上游输出标签；缺省用 --pack")
    doctor.add_argument("--pack", required=True, help="地形包版本")
    doctor.add_argument("--graph")
    doctor.add_argument("--stats")
    doctor.add_argument("--knowledge", default=str(paths.KNOWLEDGE_ROOT))
    doctor.set_defaults(handler=cmd_doctor)

    config_cmd = sub.add_parser(
        "config",
        help="显示生效的数据目录（配置文件 / 默认值）",
    )
    config_cmd.add_argument("--pack", required=True, help="地形包版本")
    config_cmd.set_defaults(handler=cmd_config)

    kinds_cmd = sub.add_parser(
        "kinds",
        help="列出动作前缀的计数口径（离散 / 可分割）与理由",
    )
    kinds_cmd.add_argument(
        "--out",
        help="写成 Markdown（例如 docs/data/action-kinds.md）；缺省打到终端",
    )
    kinds_cmd.set_defaults(handler=cmd_kinds)

    # ── data：写 data/ ────────────────────────────────────────────────

    data = sub.add_parser("data", help="把外部产物写进 data/")
    data_sub = data.add_subparsers(
        dest="data_action", required=True, metavar="<action>"
    )

    sync_cmd = data_sub.add_parser(
        "sync", help="把上游产物与原版数据复制进 data/，摆脱 ref 依赖"
    )
    sync_cmd.add_argument("--pack", required=True, help="地形包版本")
    sync_cmd.add_argument("--dry-run", action="store_true", help="只统计，不复制")
    sync_cmd.set_defaults(handler=cmd_upstream_sync)

    verify_cmd = data_sub.add_parser("verify", help="按 manifest 校验 data/ 本地副本")
    verify_cmd.add_argument("--pack", required=True, help="地形包版本")
    verify_cmd.set_defaults(handler=cmd_upstream_verify)

    import_stats = data_sub.add_parser(
        "ingest", help="世界统计产物 → data/stats/<pack>/"
    )
    import_stats.add_argument("--run", help="统计运行目录；缺省取收集侧最新一次")
    import_stats.add_argument(
        "--collector", help="采集侧 stats 根目录；缺省按 paths 解析"
    )
    import_stats.add_argument(
        "--pack", help="地形包版本；缺省从 summary 的 world 路径推断"
    )
    import_stats.add_argument(
        "--dimension",
        default="",
        help="只导入指定维度；缺省导入产物里出现的全部维度",
    )
    import_stats.add_argument("--out")
    import_stats.add_argument("--report", help="把缺口报告写到这个路径")
    import_stats.add_argument("--index", help="来源索引；给了就比对统计覆盖率")
    import_stats.set_defaults(handler=cmd_stats_import)

    loot = data_sub.add_parser(
        "loot", help="解析战利品表 → loot_expectation 统计"
    )
    loot.add_argument("--label", help="上游输出标签；缺省用 --pack-version")
    loot.add_argument("--pack-version", required=True, help="地形包版本")
    loot.add_argument(
        "--base",
        action="append",
        help="战利品表目录（可重复，后者覆盖前者）；缺省取同步下来的合并结果",
    )
    loot.add_argument("--templates", help="上游 template.jsonl；缺省取 output/<label>/facts/")
    loot.add_argument("--dimension", default="minecraft:overworld")
    loot.add_argument("--stats")
    loot.add_argument("--knowledge", default=str(paths.KNOWLEDGE_ROOT))
    loot.add_argument("--report")
    loot.set_defaults(handler=cmd_loot_build)

    # ── index：写 build/ ──────────────────────────────────────────────

    index = sub.add_parser("index", help="生成 build/ 下的索引产物")
    index_sub = index.add_subparsers(
        dest="index_target", required=True, metavar="<target>"
    )

    sources = index_sub.add_parser("sources", help="从上游图产物导出来源路径索引")
    sources.add_argument("--label", required=True, help="上游输出标签")
    sources.add_argument("--graph")
    sources.add_argument("--out")
    sources.add_argument("--stats", help="统计快照目录；缺省取 data/stats/<label>/ （meta.json + spatial/facts.jsonl）")
    sources.add_argument("--pack-version", help="统计快照的包版本；缺省用 --label")
    sources.add_argument("--knowledge", default=str(paths.KNOWLEDGE_ROOT))
    sources.set_defaults(handler=cmd_sources_build)

    slots = index_sub.add_parser(
        "slots", help="来源索引 → 槽位候选；--report 出覆盖率报告"
    )
    slots.add_argument("--label", required=True, help="上游输出标签")
    slots.add_argument("--index")
    slots.add_argument("--graph")
    slots.add_argument("--max-depth", type=int, default=MAX_DEPTH)
    slots.add_argument(
        "--report",
        action="store_true",
        help="只出覆盖率报告，不写 slots.jsonl",
    )
    slots.add_argument("--item", help="只列该物品的槽位候选（隐含 --report）")
    slots.add_argument("--out", help="构建时写 slots.jsonl；报告时写完整报告 JSON")
    slots.set_defaults(handler=cmd_slots)

    actions_cmd = index_sub.add_parser(
        "actions",
        help="全库动作图 → build/<pack>/actions.jsonl（之后 grade 只读盘）",
    )
    actions_cmd.add_argument("--pack-version", required=True, help="地形包版本")
    actions_cmd.add_argument("--stats")
    actions_cmd.add_argument("--index")
    actions_cmd.add_argument("--dimension", default="minecraft:overworld")
    actions_cmd.add_argument("--out")
    actions_cmd.set_defaults(handler=cmd_graph_build)

    return parser
