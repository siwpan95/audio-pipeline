---
name: audio-pipeline
description: Maintain the audio_pipeline orchestration tool (本 SKILL.md 所在仓库根). Four-stage music production pipeline (separate→source→mixdown→re-separate) with JSONL task log.
agent_created: true
---

# audio_pipeline 流水线维护

## 概述
纯音乐制作流水线（编排层）：拆歌 A → 单音源导出 B → 混音+分轨保留 C → 后期重分离 D。断点续跑基于产物文件判定；任意阶段失败不破坏已产物。

## 结构
- cli.py：doctor / run / stage-a..d / tasks（重型依赖惰性导入，tasks 零重依赖）
- orchestrator.py：PipelineConfig + run_pipeline（内嵌任务事件日志）
- pipelog.py：JSONL 任务日志（run_start/stage_start/stage_skip/stage_done/stage_error/run_end）
- source_maker.py / mixdown.py / separate_back.py / instrument_pool.py / manifest.py / config.py

## 常用命令
- 测试：python -m pytest tests -q（31/31，约 4 分钟；test_pipelog 5/5）
- 审计：python cli.py tasks --workspace <ws> [--json]
- 环境诊断：python cli.py doctor

## 关键约定
- 依赖隔离（R1）：torch 不进编排进程；audio_separator 经 subprocess 走其自有 venv
- 任务日志默认落盘 <workspace>/pipeline.log（与 WorldLog 同构）

## 已知陷阱
- bloodmusic.music.sampler 是**兼容层**：bloodmusic v2.0（7af5092）删除过，2026-08-23 为 audio_pipeline 恢复；若迁移到 PoolRenderer/InstrumentPool 新 API 可再删
- test_pipeline.py 依赖 bloodmusic editable 安装；重工具（demucs）全链路需真实歌曲
- 测试文件里 bloodmusic 缺失时用 _AnyModule stub（见 test_pipelog.py 头部）
