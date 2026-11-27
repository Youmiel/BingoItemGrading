"""RA 风格的路线图：item / rule 二分结构，复用 RA 的 DOT 渲染层。"""

from __future__ import annotations

from .adapter import to_path_graph
from .html import render_html, write_outputs
from .options import PathOptions, RunInfo
from .path_graph import PathGraph
from .render import build_dot, dot_to_svg, graphviz_available

__all__ = [
    "PathGraph",
    "PathOptions",
    "RunInfo",
    "build_dot",
    "dot_to_svg",
    "graphviz_available",
    "render_html",
    "to_path_graph",
    "write_outputs",
]
