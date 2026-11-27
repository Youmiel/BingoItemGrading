# 文档

按职责分三类：契约写「上游要交付什么」，数据写「一行记录长什么样」，模型写「这份数怎么算」。另有架构总览与决策记录。

## 命令

- [`command-cheatsheet.md`](command-cheatsheet.md)：逐命令参数、常见任务对照、产物位置。

## 架构

- [`architecture.md`](architecture.md)：五个阶段的数据流图、产物与命令、分层边界。

## 契约

上游两个项目与本项目的接口约定。改这里的条目要先确认上游能交付。

- [`upstream-requirements.md`](contracts/upstream-requirements.md)：上游需求总纲。
- [`collector-contract.md`](contracts/collector-contract.md)：采集器的契约缺口与数据形态。
- [`collector-conventions.md`](contracts/collector-conventions.md)：数据形态约定、我方配合项与验收顺序。
- [`upstream-changes.md`](contracts/upstream-changes.md)：当前阻塞进度的上游变更。

## 数据

统计快照与上游产物的字段含义。数值怎么来、单位是什么、哪些字段没有。

- [`sources.md`](data/sources.md)：来源索引的字段与类别。
- [`stats.md`](data/stats.md)：统计类型清单与依赖。
- [`units.md`](data/units.md)：TU、时间档位、覆盖率、分数。
- [`action-kinds.md`](data/action-kinds.md)：动作前缀的计数口径（离散 / 可分割）与理由，`bg kinds` 生成。
- [`units-conversion.md`](data/units-conversion.md)：数量缩放、交易折算、采集与熔炼折算。

## 模型

成本模型与它的边界。读之前先看 `framework-boundary.md`。

- [`framework-boundary.md`](model/framework-boundary.md)：哪些数来自数据、哪些是常量、哪些不建模。
- [`route-competition.md`](model/route-competition.md)：来源表示成动作、供给曲线、聚合与剪枝。
- [`route-competition-scoring.md`](model/route-competition-scoring.md)：方差、评分与前置处理。
- [`route-competition-order.md`](model/route-competition-order.md)：顺序约束与按预算定价。
- [`covered-register.md`](model/covered-register.md)：已接通的来源与口径索引。
- [`gap-register.md`](model/gap-register.md)：失真类缺口，P0 优先处理。
- [`gap-register-hard.md`](model/gap-register-hard.md)：硬缺口。
- [`gap-register-minor.md`](model/gap-register-minor.md)：覆盖与精度缺口。
- [`gap-register-tail.md`](model/gap-register-tail.md)：影响范围、明确不修项、待提交上游的需求。

## 决策记录

[`adr/`](adr/) 按编号排列，每条记录一个已经定下的取舍与它的代价。新增决策追加编号，不改旧条目的结论。
