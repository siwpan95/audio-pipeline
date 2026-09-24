# -*- coding: utf-8 -*-
"""musickit_chain.py —— 提示词→混音→电台 一键链（musickit S6 · 2026-08-25）

audio_pipeline 升级为总编排：新增链式流水线 P（提示词校验）→ R（渲染混音）→ B（电台开播），
复用 audio_pipeline 的 PipelineLog（run_start/stage_*/run_end，与 WorldLog 同构）。

纪律：
- 编排器绝不 import musickit 代码：各段一律子进程调用（聚合不融合）；
- 渲染器契约与 Node musickit 插件一致（末行 JSON：{ok,file,style,label,subgenre,bpm,...}）；
- 电台段 = 驱动 Node 电台 musickit 音源（POST /api/musickit play）；电台未运行不阻断（仅完成渲染）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from pipelog import PipelineLog

# 2026-09-23 去本地化：MUSICKIT_ROOT 环境变量优先，其次同级仓 repos/musickit
# （原路径为顶层分类目录，已失效）
MUSICKIT_ROOT = Path(os.environ.get("MUSICKIT_ROOT") or (Path(__file__).resolve().parent.parent / "musickit"))
RENDERER = MUSICKIT_ROOT / "remix" / "tools" / "musickit_render_one.py"
STYLE_CLI = MUSICKIT_ROOT / "music" / "style_plugin.py"
PYTHON = sys.executable


@dataclass
class ChainConfig:
    workspace: Path
    channel: str = "random"          # 混音师 slug（24 位之一 / random）
    style: str | None = None         # 提示词风格 key（63 流派之一；None=跳过校验）
    bars: int = 32
    fmt: str = "mp3"
    max_tracks: int = 5
    fusion: bool = False
    release_safe: bool = False
    radio_url: str = "http://localhost:8000"
    renderer: Path = RENDERER
    style_cli: Path = STYLE_CLI
    python: str = PYTHON
    dry_run: bool = False
    timeout: int = 900


def _run(cmd: list[str], cwd: Path, timeout: int):
    """子进程执行（Windows 隐藏窗口），返回 (exit, stdout)。"""
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=creationflags,
    )


def stage_prompt(cfg: ChainConfig) -> tuple[bool, str]:
    """P 提示词：风格 key 经 style_plugin 注册表校验（fail-loud）。"""
    if not cfg.style:
        return True, "未指定风格（跳过校验）"
    r = _run([cfg.python, str(cfg.style_cli), "--dump", "--json"], MUSICKIT_ROOT, 120)
    if r.returncode != 0:
        return False, "style_plugin 审计失败: " + (r.stderr or "").strip()[-200:]
    try:
        keys = [s["key"] for s in json.loads(r.stdout)["styles"]]
    except Exception as e:
        return False, "style_plugin 输出解析失败: " + str(e)
    if cfg.style not in keys:
        import difflib
        guess = difflib.get_close_matches(cfg.style, keys, n=3)
        return False, f"未知风格 key '{cfg.style}'" + (f"（你是指 {'/'.join(guess)}？）" if guess else "")
    return True, f"风格 {cfg.style} 校验通过（{len(keys)} 流派注册表）"


def stage_render(cfg: ChainConfig) -> tuple[bool, str, dict | None]:
    """R 混音：musickit_render_one 子进程渲染，返回 (ok, detail, meta)。"""
    if not cfg.renderer.is_file():
        return False, "找不到渲染器: " + str(cfg.renderer), None
    cmd = [cfg.python, str(cfg.renderer),
           "--channel", cfg.channel, "--bars", str(cfg.bars),
           "--format", cfg.fmt, "--out", str(cfg.workspace),
           "--max-tracks", str(cfg.max_tracks)]
    if cfg.fusion:
        cmd.append("--fusion")
    if cfg.release_safe:
        cmd.append("--release-safe")
    r = _run(cmd, MUSICKIT_ROOT, cfg.timeout)
    if r.returncode != 0:
        return False, f"渲染器退出码 {r.returncode}: " + (r.stderr or "").strip()[-200:], None
    lines = (r.stdout or "").strip().splitlines()
    if not lines:
        return False, "渲染器无输出", None
    try:
        meta = json.loads(lines[-1])
    except Exception:
        return False, "渲染器输出解析失败", None
    if not meta.get("ok") or not meta.get("file"):
        return False, meta.get("error") or "渲染器返回无效结果", None
    detail = (f"{meta.get('file')} · {meta.get('style')} · {meta.get('label')} "
              f"· {meta.get('bpm', '?')} BPM · {meta.get('duration_s', '?')}s")
    return True, detail, meta


def stage_broadcast(cfg: ChainConfig) -> tuple[bool, str]:
    """B 电台：驱动 Node 电台 musickit 音源开播；电台未运行不阻断。"""
    body = json.dumps({"action": "play", "channel": cfg.channel, "bars": cfg.bars}).encode("utf-8")
    try:
        st = urllib.request.urlopen(cfg.radio_url + "/api/status", timeout=3)
        st.read()
    except Exception:
        return True, "电台未运行（仅完成渲染，可稍后手动开播）"
    req = urllib.request.Request(cfg.radio_url + "/api/musickit", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True, f"电台已开播（Node musickit 音源，channel={cfg.channel}）"
    except urllib.error.HTTPError as e:
        return True, f"电台开播请求被拒（HTTP {e.code}），仅完成渲染"
    except Exception as e:
        return True, f"电台开播失败（{type(e).__name__}），仅完成渲染"


def run_chain(cfg: ChainConfig, log: PipelineLog | None = None) -> dict:
    """提示词→混音→电台 一键链（P→R→B，全链任务日志）。"""
    run_id = f"chain-{int(time.time())}"
    report: dict = {"stages": {}, "ok": True, "errors": [], "run_id": run_id}
    if log is None:
        log = PipelineLog(cfg.workspace / "pipeline.log")
    log.append("run_start", run_id=run_id, workspace=str(cfg.workspace),
               stages=["P", "R", "B"], channel=cfg.channel, style=cfg.style)

    for stage, fn, name in (("P", stage_prompt, "提示词校验"),
                            ("R", stage_render, "渲染混音"),
                            ("B", stage_broadcast, "电台开播")):
        log.append("stage_start", stage=stage, run_id=run_id)
        t0 = time.time()
        try:
            if stage == "R":
                ok, detail, _meta = fn(cfg)
            else:
                ok, detail = fn(cfg)
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        if not ok:
            report["ok"] = False
            report["errors"].append(f"{stage}: {detail}")
            log.append("stage_error", stage=stage, run_id=run_id,
                       error=detail, elapsed=round(time.time() - t0, 2))
        else:
            report["stages"][stage] = detail
            log.append("stage_done", stage=stage, run_id=run_id,
                       detail=detail, elapsed=round(time.time() - t0, 2))

    log.append("run_end", run_id=run_id, ok=report["ok"])
    return report
