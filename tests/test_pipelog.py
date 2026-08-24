# -*- coding: utf-8 -*-
"""
test_pipelog.py —— 流水线任务事件日志测试（P3）

覆盖：
  - PipelineLog 追加/读取/序号续写（跨实例）
  - run_pipeline 事件流：run_start → (stage_*) → run_end
    （用 stages=[] 空阶段跑轻量集成，不依赖 demucs/bloodmusic）
  - 断点续跑语义的事件表达（stage_skip 由 run_pipeline 内部判定）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PROJ_ROOT = _HERE.parent
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

import pytest  # noqa: E402

# 环境隔离：本机 bloodmusic 无 music.sampler 子模块（重工具依赖未装），
# 编排层仅模块级 import 该三符号；本测试用空 stages 不触达任何采样逻辑，
# 故在导入 orchestrator 前注入占位 stub（test_pipeline.py 同样依赖该模块）。
import types as _types  # noqa: E402
import sys as _sys  # noqa: E402


class _AnyModule(_types.ModuleType):
    """任意属性都返回空操作可调用对象，满足编排层模块级 import。"""

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: None


_fake_sampler = _AnyModule("bloodmusic.music.sampler")
_sys.modules.setdefault("bloodmusic.music.sampler", _fake_sampler)

from pipelog import PipelineLog  # noqa: E402
from orchestrator import PipelineConfig, run_pipeline  # noqa: E402


# ── PipelineLog 基础 ─────────────────────────────────

def test_pipelog_append_and_read(tmp_path: Path):
    log = PipelineLog(tmp_path / "pipeline.log")
    log.append("run_start", run_id="r1", workspace="ws")
    log.append("stage_done", stage="A", run_id="r1", detail="ok")
    entries = log.read()
    assert len(entries) == 2
    assert entries[0]["type"] == "run_start" and entries[0]["seq"] == 1
    assert entries[1]["type"] == "stage_done" and entries[1]["stage"] == "A"


def test_pipelog_seq_continues_across_instances(tmp_path: Path):
    path = tmp_path / "pipeline.log"
    PipelineLog(path).append("run_start", run_id="r1")
    e = PipelineLog(path).append("run_end", run_id="r1", ok=True)
    assert e["seq"] == 2


def test_pipelog_empty_when_no_file(tmp_path: Path):
    log = PipelineLog(tmp_path / "missing.log")
    assert log.read() == []


# ── run_pipeline 事件流（stages=[] 轻量集成） ────────

def _empty_cfg(tmp_path: Path) -> PipelineConfig:
    return PipelineConfig(workspace=tmp_path / "ws", stages=[])


def test_run_pipeline_emits_run_start_and_end(tmp_path: Path):
    cfg = _empty_cfg(tmp_path)
    report = run_pipeline(cfg)
    assert report["ok"] is True
    log_path = cfg.workspace / "pipeline.log"
    assert log_path.is_file()
    entries = PipelineLog(log_path).read()
    types = [e["type"] for e in entries]
    assert types[0] == "run_start"
    assert types[-1] == "run_end"
    assert entries[0]["stages"] == []
    assert entries[-1]["ok"] is True
    assert entries[0]["run_id"] == entries[-1]["run_id"]


def test_run_pipeline_logs_error_stage(tmp_path: Path):
    """阶段失败：stage_error 事件 + run_end ok=False。"""
    cfg = _empty_cfg(tmp_path)
    cfg.stages = ["C"]  # 无音源 → 阶段C 失败
    report = run_pipeline(cfg)
    assert report["ok"] is False
    entries = PipelineLog(cfg.workspace / "pipeline.log").read()
    types = [e["type"] for e in entries]
    assert "stage_start" in types
    assert "stage_error" in types
    assert entries[-1]["type"] == "run_end"
    assert entries[-1]["ok"] is False
    err = next(e for e in entries if e["type"] == "stage_error")
    assert err["stage"] == "C"
