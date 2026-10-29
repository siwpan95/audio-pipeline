# audio_pipeline

## 仓库角色定位（投资组合分级 ①）

> 本仓在 siwpan 投资组合（D:/projects/repos，共 133 仓）中定级为 **① 核心推进**，领域 **音乐/音频**，定位 **本地音乐流水线**。
> **角色**：拆歌→分轨→混音→重分离 制作流水线，持续投入。
> **维护策略**：① 核心推进——持续投入、不允许冻结；随所属收敛点 / 流水线长期演进。
> 投资组合统一分级索引（多机可查）：[组合分级决策索引 · allin 事实源](https://gitee.com/siwpan/allin/blob/master/docs/事实源/组合分级决策索引.md)。

> 仓库：<https://gitee.com/siwpan/audio-pipeline> ｜ 许可：MIT（见 [LICENSE](LICENSE)）
> 注：文中 <PATH> 为路径占位符，请替换为你本机实际的目录 / 可执行文件路径。

纯音乐制作流水线（编排层）—— 把「拆歌 → 单音源导出 → 混音/分轨保留 → 后期重分离」四阶段串成一条 CLI 流水线。

## 语言 / 类型
- Python（纯标准库编排 + 复用本地已装的重包）

## 目录要点
- `cli.py` —— CLI 入口（`doctor` / `run` / `stage-a` / `stage-b` / `stage-c` / `stage-d` / `tasks`）
- `orchestrator.py` —— 流水线编排（`PipelineConfig` / `run_pipeline`，内嵌任务事件日志）
- `pipelog.py` —— 任务事件日志（JSONL，与 WorldLog 同构：run_start/stage_start/stage_skip/stage_done/stage_error/run_end）
- `config.py` —— 环境引导与路径配置（`bootstrap_env` 等）
- `source_maker.py` / `mixdown.py` / `separate_back.py` / `instrument_pool.py` / `manifest.py` —— 各阶段实现
- `tests/` —— 冒烟与流水线测试；`docs/` —— 设计文档

## 运行方式与依赖
- 运行：`python <PATH> doctor`（环境诊断），
  `... cli.py run --workspace <dir> --songs <a.mp3> <b.mp3>`（完整流水线）。
- 依赖：自身不引入新重包，复用本地环境——`bloodmusic`（editable 安装）、`remix`（sys.path 注入）、
  `pydub` + `numpy`（audioop-lts 提供 Python 3.14 的 audioop），`audio_separator` 经 subprocess 调用其自有 venv。
- 详见 `requirements.txt`（仅为文档说明）。

## 任务日志（P3：日志即事实源）

每次 `run` 自动落盘 `<workspace>/pipeline.log`（JSONL，含 run_id、各阶段 start/skip/done/error、耗时与错误消息）。审计：

```bash
python cli.py tasks --workspace <工作区> [--limit 100] [--json]
```

`tasks` 不依赖重型包（bloodmusic/demucs），环境不完整时也可审计。

## 测试状态
- `py_compile` 语法检查通过（13 个 .py 文件）。
- `tests/test_pipelog.py` 5/5 通过（任务日志单元 + run_pipeline 事件流集成）。
- `tests/test_pipeline.py` 依赖 bloodmusic.music.sampler（editable 安装），本机当前缺该子模块，收集失败——**既有环境缺口，非本轮引入**。
- 重工具（依赖拆歌/混音/重分离管线）全链路试运行待 bloodmusic 环境补齐。
