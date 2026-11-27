# 命令速查

`bg` 与 `grading` 是同一个入口，等价于 `python -m bingo_grading`；命令都从仓库根运行，不依赖当前工作目录。

## 入口

- 激活 venv：PowerShell 用 `.venv\Scripts\Activate.ps1`，bash 用 `source .venv/bin/activate`。
- 没装入口脚本时用 `.venv\Scripts\python.exe -m bingo_grading <命令>`。
- 所有命令都要给地形包版本：`--pack-version`、`--pack` 或 `--label`，`26.3` 只是当前目录名。

## 常见任务

| 想做什么 | 命令 |
|---|---|
| 看一件物品多少分 | `bg grade --item diamond --table --pack-version 26.3` |
| 全库难度表落盘 | `bg grade --all --table-out build/all.md --pack-version 26.3` |
| 代价分布图 | `bg grade --all --histogram-out build/histogram.html --pack-version 26.3` |
| 看一件物品有几条路线 | `bg plan --item minecraft:diamond --paths --pack-version 26.3` |
| 画合成图 | `bg plan --item minecraft:diamond --graph-out build/graph-diamond --graph-paths 4 --graph-depth 3 --pack-version 26.3` |
| 查某个节点为什么这么贵 | `bg observe --item minecraft:iron_pickaxe --table --pack-version 26.3` |
| 查某方块的挖取代价 | `bg mining --subject minecraft:gold_ore --pack-version 26.3` |
| 查动作前缀的计数口径与理由 | `bg kinds`（`--out docs/data/action-kinds.md` 重新生成文档） |
| 查一条统计原始记录 | `bg stat --pack 26.3 --stat ore_exposure --subject minecraft:gold_ore` |
| 环境体检 | `bg doctor --pack 26.3` |
| 重建 `build/` 落盘动作图 | `bg index actions --pack-version 26.3` |
| 重建全部 `build/` 产物 | 依次 `bg index sources`、`bg index slots`、`bg index actions` |

## grade

一批物品的难度表。读 `build/<pack>/actions.jsonl`，缺失或输入过期时加 `--rebuild`。

| 参数 | 作用 |
|---|---|
| `--item 物品` | 目标物品，可重复或逗号分隔 |
| `--file 路径` | 从文件读清单，一行一个，`#` 开头为注释 |
| `--count N` | 每件需要几个，默认 1 |
| `--pack-version 版本` | 地形包版本，必填 |
| `--table` | 终端打印对齐表格 |
| `--table-out 路径` | 写表格：`.md` 带说明列、`.csv`、其它为对齐文本，可重复 |
| `--out 路径` | 结果 JSON |
| `--together` | 把 `--item` 里的物品当一个组合求一次买齐的成本 |
| `--all` | 算来源索引里的全部物品，走联合计算，带进度条 |
| `--histogram` | 终端打印代价直方图 |
| `--histogram-out 路径` | 写自包含 HTML 直方图，每箱一根柱、三个维度堆叠 |
| `--histogram-per-octave N` | 档位密度，默认 4，即每翻倍 4 箱 |
| `--rebuild` | 重建全库动作图并写盘 |
| `--stats`、`--index`、`--actions`、`--dimension` | 覆盖默认输入路径与维度 |

## plan

按供给曲线算某物品的获得路线，任意物品免登记。终端只打摘要（成本、分项、动作、
产物路径、诊断计数）；完整诊断用 `--out` 落盘或 `--json` 打到终端。

| 参数 | 作用 |
|---|---|
| `--item 物品` | 目标物品或能力 |
| `--count N` | 需要几个 |
| `--paths` | 列出最优/次优完整链，含每层原料与单价（整次调用口径） |
| `--paths-item 物品` | 额外列出中间物的链，可重复 |
| `--path-limit N` | 列前几条路线 |
| `--expand` | 展开步骤、前置与逐层曲线（写进 `--out` / `--json` 的输出里） |
| `--no-prune` | 不做 Pareto 剪枝 |
| `--out 路径` | 完整诊断写入该文件（UTF-8 JSON）。终端只打摘要 |
| `--json` | 完整诊断打到终端（机器可读） |
| `--graph-out 路径` | 写合成图基础名：`.dot`，有 Graphviz 时再写 `.svg` 与 `.html`。HTML 在图上方列出生效参数与可复跑的命令（同 RA 版式） |
| `--graph-paths N` | 合成图画前几条路线，默认 3；没画的会在图上标出条数、在表格下列出明细 |
| `--graph-depth N` | 合成图向下展开几层，默认 16（实际被树的深度封顶）；超出的节点画成虚线框，成本已计入 |
| `--graph-frontiers N` | 附「前置脆弱点」表：禁用某前置当前来源后重算整条路线，列差额最大的 N 处，默认 8，0 关闭（整次口径暂不计算） |
| `--graph-branches N` | 每个物品最多画几条候选来源，默认 3；0 = 全部，1 = 只画最优 |
| `--graph-branch-ratio R` | 候选只在「贵不到 R 倍」时才画，默认 1.5；0 = 不限 |
| `--graph-max-nodes N` | 节点总数上限，默认 500；超出的候选画成橙框虚线节点，0 = 不限 |
| `--pack-version`、`--stats`、`--index`、`--dimension` | 同上 |

## observe

摊开若干节点：价格来源、前置花费、备选来源、两套求解器对照。「绕回自身的来源」小节列出那些成本里含一份自我供给的来源（钻石块 ↔ 钻石这类互逆配方），它们报价成立但多绕一圈必然不合算。

| 参数 | 作用 |
|---|---|
| `--item 节点` | 物品名、动作 id（如 `portal:full_frame`）或假物品（如 `capability:nether_access`），可重复或逗号分隔 |
| `--table` | 终端打印对齐表格 |
| `--nearby N` | 每个节点报几件邻近价带的物品，0 关闭 |
| `--out 路径` | 结果 JSON |
| `--count`、`--pack-version`、`--stats`、`--index`、`--actions`、`--dimension` | 同上 |

## mining

挖到某方块的 TU 代价，按群系与纵段算。

| 参数 | 作用 |
|---|---|
| `--subject 方块` | 方块 ID，如 `minecraft:gold_ore` |
| `--biome 群系` | 限定群系，缺省给全部候选 |
| `--items N` | 需要几个物品 |
| `--entries-per-block N` | 每块方块的掉落量，红石矿约 4.5 |
| `--break-block 类型` | 围岩 `stone` / `deepslate`，缺省按层位自动选 |
| `--surface-y Y` | 地表高度 |
| `--expand` | 展开全部纵段明细 |
| `--pack-version`、`--stats`、`--dimension` | 同上 |

## stat

解析一条统计查询，返回值与命中的行、回退层数。

| 参数 | 作用 |
|---|---|
| `--stat 类型` | 空间测量类型；命名事实没有层级回退 |
| `--subject 主体` | 物品或方块 |
| `--biome 群系`、`--dimension 维度`、`--condition 条件` | 查询限定 |
| `--pack 版本` | 地形包版本，必填 |

## doctor 与 config

| 命令 | 作用 |
|---|---|
| `bg doctor --pack 26.3` | 检查上游产物、统计快照与知识表；`data-sources` 检查报告解析命中的层 |
| `bg config --pack 26.3` | 显示生效的数据目录，配置文件值与默认值对照 |

## data

把外部产物写进 `data/`，`data/` 不入库。

| 子命令 | 作用 |
|---|---|
| `sync --pack 版本` | 上游产物与原版数据复制进 `data/`；`--dry-run` 只统计不复制 |
| `verify --pack 版本` | 按 manifest 校验本地副本 |
| `ingest` | 采集侧运行目录 → `data/stats/<pack>/`；`--pack` 缺省从 summary 推断，`--index` 给定时比对覆盖率 |
| `loot --pack-version 版本` | 解析战利品表 → `loot_expectation`，并入 `facts.jsonl` |

## index

生成 `build/` 下的索引产物。

| 子命令 | 作用 |
|---|---|
| `sources --label 标签` | 上游图产物 → `build/<label>/source_index.jsonl` |
| `slots --label 标签` | 来源索引 → `build/<label>/slots.jsonl`；`--report` 只出覆盖率报告，`--item` 只列该物品 |
| `actions --pack-version 版本` | 全库动作图 → `build/<pack>/actions.jsonl`，之后 `grade` 只读盘 |

## 产物

| 路径 | 写入命令 |
|---|---|
| `data/upstream/<pack>/` | `bg data sync` |
| `data/stats/<pack>/` | `bg data ingest`、`bg data loot` |
| `build/<pack>/source_index.jsonl` | `bg index sources` |
| `build/<pack>/slots.jsonl` | `bg index slots` |
| `build/<pack>/actions.jsonl` | `bg index actions` |
| 难度表与直方图 | `bg grade` |
| 合成图 DOT/SVG/HTML | `bg plan --graph-out` |

`data/` 与 `build/` 都不入库，`--out` 可覆盖默认路径。

## 依赖与排错

- 合成图要 Graphviz：`dot` 不在 PATH 上时只写 `.dot`，`dot -V` 能验证。
- `bg grade` / `bg plan` 报找不到动作图时跑 `bg index actions`，或给 `--rebuild`。
- 终端中文乱码是控制台代码页问题，文件本身是 UTF-8；大输出用 `--table-out` / `--out` 落盘再读。
