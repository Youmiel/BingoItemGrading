# BingoItemGrading

Minecraft Bingo 地形包物品固有难度评分。给定一个地形包的可达性产物与一批世界统计，算出每件物品的 0–30 固有难度分，并保留「哪条路径、哪个维度、哪行统计」的追溯链。

分数描述的是物品在地形包下的固有难度，与 Bingo 卡无关；卡是下游产物。

## 当前范围

已实现：

- 世界统计层：分层 schema、快照读写、查询回退链、缓存与失效索引。
- 统计导入：读采集侧 `schemaVersion: 2` 产物（三维度群系级矿石/方块密度、纵段剖面、群系斑块、结构间距、实体、岩浆湖）转成统计行，写 `target.json` 溯源，并输出缺口报告。
- 战利品期望：自己解析战利品表（原版 + 地形包覆盖）算单箱期望，落成 `loot_expectation`。上游只管存在性，期望归本项目（ADR 0017）。
- 上游接入：把可达性图产物导出来源路径索引，带硬前置、概率、来源类别与行动路线。
- 槽位候选：来源索引 + 配方链展开，生成「物品 × 路线 × 获取方式」候选，并给出覆盖率报告。
- 难度表与直方图：任意物品从来源索引自动拼动作图，求解出主路线、备选段与整次成本，输出整次成本（TU）与直方图；直方图横轴是整次成本的对数箱，每箱一根柱、三个维度堆叠。
- 路线合成图：`bg plan --graph-out` 把前几条路线画成 DOT/SVG/HTML；图是 RecipeAnalysis 那套 item / rule 二分结构（矩形=物品、菱形=动作），渲染层直接复用它的 `PathGraph` + `build_dot`。target 的候选来源里成本最低的那条标红，其余按与最优路线的成本比由浓到浅。**成本与调用次数按整次调用口径**（与 `bg grade` 同一份递归）：不可分割的动作写「整次调用 = 52.22 TU」（多趟写「整次调用 ×12」），不会出现「调用 0.54 次」。

世界统计由两个平行项目产出：`RecipeAnalysis`（可达性图、配方 IR、原版静态数据）与 `WorldGenStatsCollector`（地形包与世界统计）。它们的**源码目录位置可配**（见下文「外部数据」），产物由 `bg data sync` 复制进 `data/`。`data/stats/<pack_version>/` 是采集侧一次运行的加工结果（含 `target.json`：种子、地形包哈希、jar 哈希），`26.3` 只是当前目录名。

`data/stats/<pack_version>/` 存成三个文件：`meta.json`（布局版本、去重后的 `note` / `derived_from` / `provenance`）、`spatial.jsonl`（可回退的空间测量）、`facts.jsonl`（按名字精确连接的命名事实）。结构、矿石、斑块、方块密度与战利品期望均已覆盖；敲对生物密度无法从静态存档测得，改由刷怪规则事实推出。

## 环境

```bash
python -m venv .venv
.venv\Scripts\activate            # 非 Windows：source .venv/bin/activate
pip install -e ".[dev]"
```

装好后有两个等价入口：`grading`（全名）与 `bg`（短别名），下文一律写 `bg`。Python 包名是 `bingo_grading`。

### 测试

```bash
pytest                            # 日常回归：约 40 秒，跳过真实大图求解
pytest -m slow                    # 只跑真实大图上的整图/子图一致性（约 23 秒）
pytest -m "slow or not slow"      # 全量：约 66 秒
```

`slow` 标记的两条用例要在落盘动作图（`build/26.3/actions.jsonl`）上做整图求解，单条 10 秒以上；它们验的是「子图优化与全图结果一致」，改动求解器或子图构造时才需要跑。

需要 Python 3.11+。

### 外部数据

所有外部数据的路径由 `src/bingo_grading/paths.py` 唯一解析，优先级是 **显式参数 > `data/` 自持**（ADR 0025）。
运行态**不回退到上游项目**：`ref/` 是不入库的本地检出、没有可迁移性，缺 `data/` 就是缺数据，
由命令报缺口，而不是悄悄去读开发者本机的别的目录。`config.yml` 里的两个上游目录只被
`bg data sync` / `bg data verify` 使用。

**本仓库自己的目录不可配**，它们是 `src/bingo_grading/paths.py` 常量区里的固定值（`data/` 及其子树、`build/`）。
配置里只有两项——两个上游项目的源码目录（只用于同步）：

```bash
cp config.example.yml config.yml    # config.yml 不入库
```

```yaml
paths:
  recipe_analysis: '../RecipeAnalysis'          # RecipeAnalysis 仓库根
  collector: '../WorldGenStatsCollector'        # WGS 仓库根
```

两项都必填、没有默认值；缺文件、缺项、空值、未知项都会在读配置时报错退出。
相对路径按配置文件所在目录解析。查找顺序是环境变量 `BG_CONFIG` → 当前工作目录 → 仓库根。
`~` 会展开。

值是**标准 YAML**（PyYAML 解析），注释、引号、多行都按规范来。唯一一处定制：
所有标量都按字符串读，不做隐式类型推断——否则 `26.3` 会变 float、`on` 会变 bool。
路径一律用**单引号**包起来：YAML 的单引号不做转义，Windows 路径里的反斜杠可以
原样写；双引号会把 `\g`、`\R` 当非法转义而报错。

```bash
bg config --pack 26.3            # 上游两项目录 + 本仓库固定目录的生效路径与是否存在
```

`data/` 下**只有 `knowledge/` 入库**。
`upstream/` 是 `bg data sync` 从两个上游项目复制出来的副本，入库等于自己
维护一份副本，上游更新后两边就分叉了；
`stats/` 是 `bg data ingest` 与 `bg data loot` 从 `upstream/` 加工出来的快照，
同样可重建。
`knowledge/` 是人工维护的分类映射，上游没有对应物，必须入库。

| 类别 | 位置 | 源 | 入库 |
|---|---|---|---|
| 合并后的数据包 | `data/upstream/<pack>/datapack/` | `RecipeAnalysis/inputs/<pack>/datapack-merged/data/`（原版 ⊗ 地形包 overlay 的生效结果，整个约 78 MB） | 否 |
| 上游产物 | `data/upstream/<pack>/` | `RecipeAnalysis/output/<pack>/` 与采集侧 `stats/<run>/` | 否 |

**地形包是原版地形的增量补丁**，只覆盖它关心的少数文件（当前包只有 2 张战利品表），其余一律沿用原版。包内还按 `pack.mcmeta` 声明的数据包格式区间分层（overlay），哪一层生效由原版 `version.json` 的 `pack_version.data_major/minor` 决定，**不是目录名**。

**overlay 判定与逐层覆盖由 RecipeAnalysis 完成**，产物是 `inputs/<pack>/datapack-merged/`；本项目**整包同步**它的 `data/`（不挑文件、不按类型过滤），用的时候从里面取 `minecraft/loot_table`。不保留地形包原件，也不重复实现合并语义。详见 ADR 0029。

**战利品表的解析与 RecipeAnalysis 共用** `src/mcshared/`（语法归一：两种方言、条件/函数、条目树）。
上游客仓库通过 junction 以 `ra.mcshared` 引用同一份代码。语义与概率不共用：上游按存在性求解，本侧才计算数量。详见 ADR 0026、ADR 0027。

配方的 IR（`recipe_slots.py` / `recipe_ir.py`）是**同构副本**：原始实现在 RA 的
`src/ra/ir/` 下，本仓库 `src/mcshared/` 里放接口与行为一致的一份，改动两边同步；
后续抽成独立包后再取消同步。

克隆后按顺序重建 `data/`（约 21 MB，需要 `config.yml` 指的两个上游项目在场）：

```bash
bg data sync --pack 26.3 --dry-run     # 先看要复制什么
bg data sync --pack 26.3               # 复制并写 manifest
bg data verify --pack 26.3             # 校验内容摘要
bg data ingest --pack 26.3             # 采集侧运行目录 → data/stats/<pack>/（meta + spatial + facts）
bg data loot --pack-version 26.3       # 战利品期望 → loot_expectation，并入 facts.jsonl
```

`bg doctor` 的 `data-sources` 检查报告当前解析命中了哪一层：全部 `local` 才算真正摆脱对上游目录的运行时依赖。

## 命令

所有命令都要显式给地形包版本（`--pack-version` / `--pack` / `--label`）；下面的 `26.3` 只是示例，换成你的包版本。逐命令参数、常见任务对照与产物位置见 [docs/command-cheatsheet.md](docs/command-cheatsheet.md)。

```bash
bg doctor --pack 26.3                      # 检查上游产物、统计快照与知识表
bg index actions --pack-version 26.3       # 全库动作图 → build/<pack>/actions.jsonl
bg grade --all --table-out build/all.md --pack-version 26.3
bg grade --all --histogram-out build/histogram.html --pack-version 26.3
bg plan --item minecraft:diamond --graph-out build/graph-diamond --graph-paths 4 --graph-depth 3 --pack-version 26.3
bg observe --item minecraft:iron_pickaxe --table --pack-version 26.3
```

产物默认写到仓库根的 `build/`，可用 `--out` 覆盖。命令不依赖当前工作目录。

## 数据流

```
上游工作目录 ──data sync──→ data/upstream/<pack>/（manifest 校验）
                                          │
上游 graph.json ──index sources──→ source_index.jsonl ──┐
                                                         ├─index slots──→ slots.jsonl
上游配方规则  ─────────────────────────────────────────┘
source_index.jsonl ──index actions──→ actions.jsonl ──grade──→ 难度表（TU / 分数 / 可信度）
收集侧 stats/<run>/ ──data ingest──→ data/stats/<pack>/（spatial.jsonl / facts.jsonl）
spatial.jsonl ──stat──→ 值 + 命中的行 + 回退层数
data/knowledge/*.toml + stats ──plan──→ 代价聚合（TU / 亮度 / 等级）
strategies/ ──（未实现）──→ path_node / edge
```

## 目录

| 路径 | 内容 |
|---|---|
| `data/` | **输入**：上游同步的原始数据、统计快照、手工表 |
| `data/upstream/` | 由 `bg data sync` 从两个上游项目复制，不入库 |
| `data/stats/<pack_version>/` | 统计快照：由 `bg data ingest` 重建，不入库 |
| `data/knowledge/` | 手工知识表：路线集合、战利品表→结构集合映射（入库） |
| `build/` | **输出**：来源索引、动作图、槽位与各类报告，不入库 |
| `src/bingo_grading/paths.py` | 外部路径的唯一解析入口：项目自有目录是常量，上游目录读 `config.yml` |
| `src/bingo_grading/upstream.py` | 上游产物同步与 manifest 校验 |
| `src/bingo_grading/case/` | 取数算子与动作工厂，入口 `registry.builder_for()` |
| `src/bingo_grading/stats/` | 统计模型、快照读写、查询解析、失效索引、外部产物导入 |
| `src/bingo_grading/sources/` | 上游产物读取、来源类别判定、前置闭包、路线判定 |
| `src/bingo_grading/recipes/` | 配方链展开到非合成来源 |
| `src/bingo_grading/loot/` | 战利品表分层加载与期望计算（语法层在 `src/mcshared/`） |
| `src/mcshared/` | 与 RecipeAnalysis 共用的数据包解析层，物理只有一份 |
| `src/bingo_grading/slots/` | 槽位聚合与覆盖率报告 |
| `docs/` | 契约、数据口径、模型与架构文档，分类见 `docs/README.md` |
| `tools/` | 一次性脚本，不作运行入口 |
| `tests/` | 分组测试，不依赖上游项目（`conftest.py` 自动生成临时配置） |
## 文档

分类索引见 [`docs/README.md`](docs/README.md)。最常查的几份：

- 需要上游改什么：[`docs/contracts/upstream-changes.md`](docs/contracts/upstream-changes.md)
- 代码结构与数据流：[`docs/architecture.md`](docs/architecture.md)
- 框架边界（哪些数来自数据、哪些不建模）：[`docs/model/framework-boundary.md`](docs/model/framework-boundary.md)
- 未补的缺口：[`docs/model/gap-register.md`](docs/model/gap-register.md)
- 设计决策：[`docs/adr/`](docs/adr/)