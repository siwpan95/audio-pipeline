# -*- coding: utf-8 -*-
"""
pipelog.py —— 流水线任务事件日志（日志即事实源，与 WorldLog 同构）

追加式 JSONL：每行一条事件 {seq, ts, type, stage, run_id, ...}。
事件类型：
  run_start    流水线开始（记录 run_id / workspace / 请求的阶段）
  stage_start  阶段开始
  stage_done   阶段完成（detail=产物摘要）
  stage_skip   阶段跳过（断点续跑 / 无输入）
  stage_error  阶段失败（error 消息）
  run_end      流水线结束（ok / 错误数）

默认落盘 <workspace>/pipeline.log —— 与产物同区，审计/断点续跑有据可查。
CLI 审计：cli.py tasks <workspace> [--limit N] [--json]。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

__all__ = ["PipelineLog"]


class PipelineLog:
    """追加式流水线任务日志（线程安全）。"""

    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._seq = 0

    # ── 写入 ──────────────────────────────────────────

    @property
    def path(self) -> Path:
        return self._path

    def append(self, event_type: str, stage: Optional[str] = None,
               run_id: Optional[str] = None, **payload: Any) -> dict:
        """追加一条事件，返回条目。"""
        with self._lock:
            if self._seq == 0:
                self._seq = self._read_max_seq()
            self._seq += 1
            entry: dict = {
                "seq": self._seq,
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "type": event_type,
            }
            if run_id:
                entry["run_id"] = run_id
            if stage:
                entry["stage"] = stage
            entry.update(payload)
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError:
                pass  # 日志失败不阻断流水线
            return entry

    def _read_max_seq(self) -> int:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                seq = 0
                for line in f:
                    try:
                        seq = max(seq, int(json.loads(line).get("seq", 0)))
                    except (ValueError, json.JSONDecodeError):
                        continue
                return seq
        except OSError:
            return 0

    # ── 读取（审计） ──────────────────────────────────

    def read(self, limit: int = 100) -> list[dict]:
        """读取最近 limit 条事件。"""
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return []
        entries: list[dict] = []
        for line in lines[-limit:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries

    @staticmethod
    def new_run_id() -> str:
        """生成一次运行标识（短 uuid）。"""
        return uuid.uuid4().hex[:8]


def default_log_path(workspace: Path | str) -> Path:
    """工作区默认日志路径：<workspace>/pipeline.log。"""
    return Path(workspace) / "pipeline.log"
