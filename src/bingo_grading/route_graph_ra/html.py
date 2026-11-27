"""路线图的外层 HTML 与落盘。

复用 RA 的 `build_dot` 生成图，但保留本项目的「路线对照表」——RA 的
`render_html` 统计的是折叠数、假设数这些本仓库没有的指标。

开头文字只留**读图必需**的几条；固定成本去重、深度截断、回边这些一次性
说明收进可展开区，避免每次看图先读一段散文。
"""

from __future__ import annotations

import html
from pathlib import Path

from ..plan.explain import PathTree
from .adapter import to_path_graph
from .colors import cost_cap, route_color, route_ink
from .options import PathOptions, RunInfo
from .render import build_dot, dot_to_svg


def _run_block(run: RunInfo | None) -> str:
    """生成参数：照 RA 的做法——图上方、grid 铺开、不折叠。

    RA 的理由是「这张表的用途是复算」，折叠就违背了它。本项目的参数比 RA
    多一层（`bg plan` 前缀），所以 grid 之外再给一条可整段复制的命令。
    """
    if run is None:
        return ""
    cells = "".join(
        f"<span>{html.escape(name)} {html.escape(value)}</span>"
        for name, value in run.entries
    )
    fingerprint = ""
    if run.action_count or run.stats_digest:
        pieces = []
        if run.action_count:
            pieces.append(f"动作图 {run.action_count} 条")
        if run.stats_digest:
            pieces.append(f"统计摘要 <code>{html.escape(run.stats_digest[:12])}</code>")
        fingerprint = f'<div class="fingerprint">输入：{" · ".join(pieces)}</div>'
    return f"""<div class="label">生成参数</div>
<pre class="cmd">{html.escape(run.command)}</pre>
<div class="params">{cells}</div>
{fingerprint}
"""


def _frontier_table(frontiers: list[dict]) -> str:
    """「换个前置来源要贵多少」。空表不占位。"""
    if not frontiers:
        return ""
    rows = "".join(
        f'<tr><td>{html.escape(row["item"].split(":", 1)[-1])}</td>'
        f'<td>{html.escape(row["action"])}</td>'
        f'<td>{html.escape(row["alternative"] or "（无替代来源）")}</td>'
        f'<td>{row["route_total"]:.2f} TU</td>'
        f'<td>+{row["delta"]:.2f}（+{row["delta_ratio"] * 100:.1f}%）</td></tr>'
        for row in frontiers
    )
    return f"""
<h2>前置替换成本</h2>
<div class="sub">
  口径：禁用该前置当前的来源后<b>重新求解整条路线</b>，差额含固定成本去重与多级依赖的
  真实变化，不是各层单价相加。「换成什么」是求解器在缺这个来源时的最优选择。
</div>
<table><thead><tr><th>前置</th><th>当前来源</th><th>换成什么</th><th>整条路线</th>
<th>差额</th></tr></thead><tbody>{rows}</tbody></table>
"""


def render_html(
    svg_text: str,
    trees: list[PathTree],
    target: str,
    *,
    frontiers: list[dict] | None = None,
    run: RunInfo | None = None,
    counts: dict[str, int] | None = None,
    skipped_paths: tuple[tuple[int, float, float, str], ...] = (),
) -> str:
    """自包含 HTML：统计摘要 + 图例 + 生成参数 + SVG + 路线对照表。

    版式照 RA：图的规模与生成参数都在**图之上**，参数 grid 铺开、不折叠
    ——读者先知道「这是多大、用什么参数画的」，再往下看图。

    `skipped_paths` 是没画出来的路线（`--graph-paths` 截掉的）：表格下注明还有
    几条、最便宜的那条是什么，否则「路线对照」看起来像是全部候选。
    """
    best = min((tree.total_cost for tree in trees if tree.total_cost > 0), default=0.0)
    cap = cost_cap(trees, best)
    rows = []
    for index, tree in enumerate(sorted(trees, key=lambda item: item.rank)):
        ratio = tree.total_cost / best if best > 0 else 1.0
        # 并列最优只给第一条红色锚点，其余同价路线见对照表。
        chip = route_ink(ratio, cap) if index == 0 else route_color(ratio, cap)
        rows.append(
            f'<tr><td>#{tree.rank}</td>'
            f'<td><span class="chip" style="background:{chip}"></span>{ratio:.3f}×</td>'
            f"<td>{tree.unit_cost:.2f} TU</td>"
            f"<td>{tree.total_cost:.2f} TU</td>"
            f"<td>{tree.rank_gap:+.2f}</td>"
            f"<td>{'(最优)' if tree.rank == 1 else ''}</td></tr>"
        )
    body = "".join(rows)
    name = html.escape(target.split(":", 1)[-1])
    parts = [
        f"{label} {value} 个" for label, value in (counts or {}).items()
    ]
    summary = "、".join(parts) + "。" if parts else ""
    summary += "箭头一律「来源 → 产物」，从上往下读是一条获取路径。"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>合成图 · {name}</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ margin: 0; padding: 28px 32px 48px; background: #fbfbfc; color: #22262a;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif; }}
h1 {{ font-size: 19px; margin: 0 0 4px; }}
h2 {{ font-size: 14px; margin: 22px 0 8px; }}
.sub {{ color: #8a9099; font-size: 13px; margin-bottom: 18px; line-height: 1.7; }}
.card {{ background: #fff; border: 1px solid #e6e8eb; border-radius: 10px;
  padding: 14px; box-shadow: 0 1px 2px rgba(16,24,40,.04); overflow: auto; }}
table {{ border-collapse: collapse; font-size: 13px; margin: 12px 0 4px; }}
th, td {{ text-align: left; padding: 5px 16px 5px 0; border-bottom: 1px solid #eef0f2; }}
th {{ color: #8a9099; font-weight: 600; }}
td code {{ font-size: 12.5px; }}
.chip {{ display: inline-block; width: 22px; height: 10px; border-radius: 3px;
  margin-right: 7px; vertical-align: 0; }}
.warn {{ background: #fff7ed; border: 1px solid #fdba74; color: #9a3412;
  padding: 10px 12px; border-radius: 8px; font-size: 13px; }}
.cmd {{ background: #0f172a; color: #e2e8f0; border-radius: 8px; padding: 10px 14px;
  font-family: "Cascadia Mono", Consolas, monospace; font-size: 12.5px;
  white-space: pre-wrap; word-break: break-all; margin: 0 0 8px; }}
.params {{ display: grid; gap: 2px 20px;
  grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
  font-family: "Cascadia Mono", Consolas, monospace; font-size: 12.5px;
  color: #475569; background: #fff; border: 1px solid #e6e8eb; border-radius: 8px;
  padding: 8px 12px; margin-bottom: 6px; }}
.fingerprint {{ color: #8a9099; font-size: 12px; margin-bottom: 6px; }}
.label {{ color: #8a9099; font-size: 12px; margin: 14px 0 6px; }}
.legend {{ color: #5b6270; font-size: 13px; line-height: 1.9; margin-bottom: 2px; }}
.legend details > summary {{ cursor: pointer; color: #8a9099; font-size: 12.5px; }}
</style></head><body>
<h1>合成图 · {name}</h1>
<div class="sub">{summary}</div>
<div class="legend">
  <b>椭圆 = 物品</b>（多条入边 = 多个候选来源），<b>矩形 = 一条动作</b>（多条入边 = 并列前置，缺一不可）；
  箭头是「来源 → 产物」，从上往下读。
  <b>红色 = 最优路线</b>；其余来源按与最优的成本比由浓到浅。
  数字按<b>整次调用</b>计，写成上下对齐的三行：<b>总代价</b>、各项数值、各项名称；
  <b>同一节点的各项数值相加等于它的总代价</b>，根节点的数等于 target 边上的数字；
  名称后缀<b>（已付）</b>的一项记 0 —— 同一个一次性前置（工具、站点）整条路线只收一趟。
  橙框虚线 = 还有东西没画（tooltip 与下方说明里写清是哪几条）。
  「未画候选」挂在物品上（这个物品还有别的来源）；「未画路线」挂在 target 上
  （`--graph-paths` 只画了前几条整条路线）。
  <details><summary>更多（跨节点求和、深度截断、回边、并列最优）</summary>
    <b>跨节点</b>看，把子节点的总代价直接相加可能大于父节点：批量产出的动作
    （打一只铁傀儡给 4 个铁锭）在两个分支里各按一整次记了一次，实际一次就够；
    一次性前置则已经去重，不会重复。
    虚线框 = 展到深度上限，成本已计入、只是不再展开更下游。
    虚线<b>回边</b> = 这一步要用的东西本页正在造（如钻石块 ↔ 钻石），那一份按该物品的最优价计。
    并列最优只标红第一条，其余同价路线见下方对照表，名次会并列。
  </details>
</div>
{_run_block(run)}
<div class="card">{svg_text}</div>
<h2>路线对照</h2>
<table><thead><tr><th>名次</th><th>成本比</th><th>单价</th><th>整次</th>
<th>比最优</th><th></th></tr></thead><tbody>{body}</tbody></table>
{_skipped_paths_note(skipped_paths, drawn=len(trees))}
{_frontier_table(frontiers or [])}
</body></html>
"""


def _skipped_paths_note(
    skipped: tuple[tuple[int, float, float, str], ...], *, drawn: int
) -> str:
    """「路线对照」下面那句：只画了前 N 条时，说清还有几条、最便宜的是哪条。

    没有这句，表格看起来就是全部候选——而 `--graph-paths` 默认只画 3 条，
    `diamond` 实际有 26 条。
    """
    if not skipped:
        return ""
    count = len(skipped)
    first_rank, first_ratio, first_cost, first_action = skipped[0]
    listed = "".join(
        f"<li>#{rank} <code>{html.escape(action_id)}</code>"
        f" —— {ratio:.2f}×，{cost:.2f} TU</li>"
        for rank, ratio, cost, action_id in skipped[:20]
    )
    more = f"<li>…另 {count - 20} 条</li>" if count > 20 else ""
    total = drawn + count
    return (
        f'<p class="warn">图与上表只画了前 {drawn} 条路线。还有 <b>{count}</b> 条没画，'
        f"最便宜的是 #{first_rank} <code>{html.escape(first_action)}</code>"
        f"（{first_ratio:.2f}×，{first_cost:.2f} TU）。"
        f"整个命令加 <code>--graph-paths {total}</code> 可把它们也画出来。"
        f"<details><summary>未画的 {count} 条（按代价从低到高）</summary>"
        f"<ol>{listed}{more}</ol></details></p>"
    )


def write_outputs(
    base: Path,
    trees: list[PathTree],
    target: str,
    *,
    rankdir: str = "TB",
    frontiers: list[dict] | None = None,
    run: RunInfo | None = None,
    solved=None,
    actionset=None,
    branches: int = 0,
    max_nodes: int = 0,
    ratio_limit: float = 0.0,
    skipped_paths: tuple[tuple[int, float, float, str], ...] = (),
) -> list[str]:
    """写 `<base>.dot`，能画 SVG 时再写 `<base>.svg` 与 `<base>.html`。

    默认 `rankdir=TB`：来源在上、产物沉到最下面，读图顺序与施工顺序一致。
    `run` 是生成参数，写进 HTML 供复现。`skipped_paths` 是没画出来的路线。
    """
    if not trees:
        return []
    stem = base.with_suffix("") if base.suffix else base
    if stem.parent and not stem.parent.exists():
        stem.parent.mkdir(parents=True, exist_ok=True)
    graph = to_path_graph(
        trees,
        target,
        rankdir=rankdir,
        solved=solved,
        actionset=actionset,
        branches=branches,
        max_nodes=max_nodes,
        ratio_limit=ratio_limit,
        skipped_paths=skipped_paths,
    )
    dot_text = build_dot(graph, PathOptions(rankdir=rankdir))
    dot_path = stem.with_suffix(".dot")
    dot_path.write_text(dot_text, encoding="utf-8", newline="\n")
    written = [str(dot_path)]
    svg = dot_to_svg(dot_text)
    if svg is not None:
        svg_path = stem.with_suffix(".svg")
        svg_path.write_text(svg, encoding="utf-8", newline="\n")
        written.append(str(svg_path))
        svg_text = svg[svg.find("<svg"):] if "<svg" in svg else svg
    else:
        svg_text = (
            '<p class="warn">没有找到 <code>dot</code>，无法生成 SVG。'
            "DOT 文件已写出，装好 Graphviz 后重新生成即可。</p>"
        )
    html_path = stem.with_suffix(".html")
    html_path.write_text(
        render_html(
            svg_text,
            trees,
            target,
            frontiers=frontiers,
            run=run,
            counts={
                "图形节点": len(graph.nodes),
                "动作": len(graph.rules),
                "边": len(graph.edges),
                "未画候选": sum(graph.omitted.values()),
            }
            | ({"未画路线": len(skipped_paths)} if skipped_paths else {}),
            skipped_paths=skipped_paths,
        ),
        encoding="utf-8",
        newline="\n",
    )
    written.append(str(html_path))
    return written
