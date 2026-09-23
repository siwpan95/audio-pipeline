# -*- coding: utf-8 -*-
"""
orchestrator.py —— 四阶段全链路编排（T6）

职责：
  - PipelineConfig：流水线全局配置数据类
  - run_pipeline(cfg)：串联阶段 A→B→C→D
      * 基于 manifest / 产物文件判定完成度实现断点续跑（P1-4）
      * 任意阶段失败记录异常但不破坏已产物（错误追加至 report.errors，继续后续阶段）
      * 工作区目录自动创建

依赖隔离：所有阶段仅通过模块调用 + subprocess（audio_separator）执行，
          不来 import torch 进编排进程。
设计依据：docs/design.md §2.6、§6.1、§7（决策① 独立编排工具）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from pipelog import PipelineLog, default_log_path

from config import (
    bootstrap_env,
    DEFAULT_SEP_MODEL,
    DEFAULT_SEP_SHIFTS,
    default_midi_root,
)
from instrument_pool import (
    InstrumentPool,
    build_instrument_pool,
    _discover_existing_stems,
)
from source_maker import TrackSpec, make_source
from mixdown import mixdown, MixResult
from manifest import read_manifest
from separate_back import separate_back, SeparateMode
from core.audio_mixer import MixConfig

bootstrap_env()


@dataclass
class PipelineConfig:
    """流水线全局配置（决策⑦ --separate-mode 默认 manifest）。"""

    workspace: Path                              # audio_pipeline_workspace
    songs: list[Path] = field(default_factory=list)    # 阶段A 输入真实歌
    midi_root: Path = field(default_factory=default_midi_root)   # 阶段B MIDI 素材根（env MIDI_ROOT → 同级仓 repos/midi）
    sep_model: str = DEFAULT_SEP_MODEL
    sep_shifts: int = DEFAULT_SEP_SHIFTS
    mix_config: MixConfig = field(default_factory=MixConfig)   # 透传增益/归一化/削波/淡变/压缩/滤波
    separate_mode: SeparateMode = "manifest"     # 决策⑦ 默认模式A
    stages: list[str] = field(default_factory=lambda: ["A", "B", "C", "D"])
    track_specs: list[TrackSpec] = field(default_factory=list)  # 阶段B 规格
    mix_fmt: str = "wav"                         # 混音成品格式


def _stems_from_spec(out_dir: Path, spec: TrackSpec) -> Path:
    """按 §6.2 推断阶段B 预期产物文件名（不创建目录，用于断点续跑判定）。"""
    instrument_class = (
        spec.pool_key if spec.kind == "sampled" and spec.pool_key else "synth"
    )
    return out_dir / f"{instrument_class}__{spec.name}.{spec.fmt}"


def _gather_stems(stems_dir: Path) -> list[Path]:
    """收集 stems 目录下已存在的音源文件。"""
    stems: list[Path] = []
    if stems_dir.is_dir():
        for ext in ("*.wav", "*.ogg", "*.flac"):
            stems.extend(stems_dir.glob(ext))
    return sorted(stems)


def _emit_stage_finish(log: PipelineLog, run_id: str, stage: str,
                           t0: float, report: dict) -> None:
    """阶段成功收尾：断点续跑 → stage_skip，否则 stage_done。"""
    detail = str(report["stages"].get(stage, ""))
    event = "stage_skip" if detail.startswith("skipped") else "stage_done"
    log.append(event, stage=stage, run_id=run_id,
               duration_s=round(time.time() - t0, 2), detail=detail)


def run_pipeline(cfg: PipelineConfig, log: PipelineLog | None = None) -> dict:
    """串联阶段 A→B→C→D，返回结构化报告。

    断点续跑（P1-4）：
      - 阶段A：若各 song 的目标目录已存在 stems，跳过 demucs；
      - 阶段B：按 TrackSpec 推断预期文件名，已存在则跳过单条渲染；
      - 阶段C：若 manifest.json 与 song_final 均存在，跳过混音；
      - 阶段D：若 separated/ 下产物已就绪，跳过。

    异常处理：每阶段 try/except，失败记录到 report.errors，
             不中断后续阶段（不破坏已产物）。

    Args:
        cfg: PipelineConfig。

    Returns:
        dict: {"stages": {...}, "ok": bool, "errors": [...]}
    """
    ws = Path(cfg.workspace)
    fixtures_dir = ws / "instruments"
    stems_dir = ws / "stems"
    mixdown_dir = ws / "mixdown"
    separated_dir = ws / "separated"

    # 创建工作区子目录（设计 §6.1）
    for d in (fixtures_dir, stems_dir, mixdown_dir, mixdown_dir / "_sources", separated_dir):
        d.mkdir(parents=True, exist_ok=True)

    # ── 任务事件日志（P3：日志即事实源，默认 <workspace>/pipeline.log） ──
    if log is None:
        log = PipelineLog(default_log_path(ws))
    run_id = PipelineLog.new_run_id()
    log.append("run_start", run_id=run_id, workspace=str(ws), stages=cfg.stages)

    report: dict = {"stages": {}, "ok": True, "errors": []}
    pool: InstrumentPool | None = None
    stems: list[Path] = []
    mix_result: MixResult | None = None

    # ═══════════════════════ 阶段 A — 乐器池 ═══════════════════════
    if "A" in cfg.stages:
        _t0 = time.time()
        log.append("stage_start", stage="A", run_id=run_id)
        try:
            if cfg.songs:
                # 断点续跑：全部 song 均已产出 stems 则跳过
                all_seeded = all(
                    _discover_existing_stems(fixtures_dir / Path(s).stem)
                    for s in cfg.songs
                )
                if all_seeded:
                    report["stages"]["A"] = "skipped (already separated)"
                else:
                    pool = build_instrument_pool(
                        cfg.songs, fixtures_dir,
                        model=cfg.sep_model, shifts=cfg.sep_shifts,
                    )
                    n = sum(len(v) for v in pool.samples_by_key.values())
                    report["stages"]["A"] = f"built pool ({n} samples across {len(pool.samples_by_key)} families)"
            else:
                report["stages"]["A"] = "skipped (no input songs)"
            # 无论走哪条路径，最终加载池供后续阶段使用
            pool = InstrumentPool.load(fixtures_dir)
        except Exception as exc:
            report["ok"] = False
            report["errors"].append(("A", str(exc)))
            print(f"[阶段A 失败] {exc}")
            log.append("stage_error", stage="A", run_id=run_id, error=str(exc))
        else:
            _emit_stage_finish(log, run_id, "A", _t0, report)
    else:
        # 未在 stages 列表中但可能仍需池 → 尝试从已有目录加载
        if fixtures_dir.is_dir():
            pool = InstrumentPool.load(fixtures_dir)

    # ═══════════════════════ 阶段 B — 单音源导出 ═══════════════════════
    if "B" in cfg.stages:
        _t0 = time.time()
        log.append("stage_start", stage="B", run_id=run_id)
        try:
            if pool is None:
                pool = InstrumentPool(fixtures_dir)
            new_count = 0
            skip_count = 0
            for spec in cfg.track_specs:
                expected = _stems_from_spec(stems_dir, spec)
                if expected.is_file():
                    # 断点续跑：单条音源已产出
                    stems.append(expected)
                    skip_count += 1
                    continue
                out = make_source(spec, pool, stems_dir)
                stems.append(out)
                new_count += 1
            if new_count == 0 and skip_count > 0:
                report["stages"]["B"] = f"skipped ({skip_count} stems already exist)"
            else:
                report["stages"]["B"] = (
                    f"produced {new_count} new, {skip_count} existing → {len(stems)} stems"
                )
        except Exception as exc:
            report["ok"] = False
            report["errors"].append(("B", str(exc)))
            print(f"[阶段B 失败] {exc}")
            log.append("stage_error", stage="B", run_id=run_id, error=str(exc))
        else:
            _emit_stage_finish(log, run_id, "B", _t0, report)
    else:
        stems = _gather_stems(stems_dir)

    # ═══════════════════════ 阶段 C — 混音 + 分轨保留 ═══════════════════════
    if "C" in cfg.stages:
        _t0 = time.time()
        log.append("stage_start", stage="C", run_id=run_id)
        try:
            manifest_path = mixdown_dir / "manifest.json"
            if manifest_path.is_file():
                mani = read_manifest(manifest_path)
                song_path = mixdown_dir / mani.song
                if song_path.is_file():
                    mix_result = MixResult(
                        song_path=song_path,
                        sources_dir=mixdown_dir / "_sources",
                        manifest_path=manifest_path,
                    )
                    report["stages"]["C"] = "skipped (manifest + song_final exist)"
                else:
                    # manifest 有但缺成品歌 → 重新混
                    mix_result = _do_mixdown(stems_dir, mixdown_dir, cfg, stems)
                    report["stages"]["C"] = f"mixed → {mix_result.song_path.name}"
            else:
                mix_result = _do_mixdown(stems_dir, mixdown_dir, cfg, stems)
                report["stages"]["C"] = f"mixed → {mix_result.song_path.name}"
        except Exception as exc:
            report["ok"] = False
            report["errors"].append(("C", str(exc)))
            print(f"[阶段C 失败] {exc}")
            log.append("stage_error", stage="C", run_id=run_id, error=str(exc))
        else:
            _emit_stage_finish(log, run_id, "C", _t0, report)

    # ═══════════════════════ 阶段 D — 后期重分离 ═══════════════════════
    if "D" in cfg.stages:
        _t0 = time.time()
        log.append("stage_start", stage="D", run_id=run_id)
        try:
            manifest_path = mixdown_dir / "manifest.json"
            if not manifest_path.is_file():
                raise FileNotFoundError(
                    "阶段D 需要 manifest.json（请先运行阶段C）。"
                )

            if cfg.separate_mode == "manifest":
                # 断点续跑：全部条目已在 separated/ 则跳过
                mani = read_manifest(manifest_path)
                all_exist = all(
                    (separated_dir / e.output_name).is_file()
                    for e in mani.sources
                )
                if all_exist:
                    report["stages"]["D"] = "skipped (already separated)"
                else:
                    song = (
                        mix_result.song_path
                        if mix_result is not None
                        else mixdown_dir / mani.song
                    )
                    res = separate_back(
                        song, separated_dir,
                        mode="manifest", manifest=manifest_path,
                    )
                    report["stages"]["D"] = (
                        f"separated (manifest, 无损) → {len(res['files'])} files"
                    )
            else:  # "algo"
                mani = read_manifest(manifest_path)
                song = (
                    mix_result.song_path
                    if mix_result is not None
                    else mixdown_dir / mani.song
                )
                produced = separated_dir / cfg.sep_model / song.stem
                if produced.is_dir() and any(produced.glob("*.wav")):
                    report["stages"]["D"] = "skipped (already separated)"
                else:
                    res = separate_back(
                        song, separated_dir,
                        mode="algo",
                        model=cfg.sep_model,
                        shifts=cfg.sep_shifts,
                    )
                    report["stages"]["D"] = (
                        f"separated (algo, 有损) → {len(res['files'])} files"
                    )
        except Exception as exc:
            report["ok"] = False
            report["errors"].append(("D", str(exc)))
            print(f"[阶段D 失败] {exc}")
            log.append("stage_error", stage="D", run_id=run_id, error=str(exc))
        else:
            _emit_stage_finish(log, run_id, "D", _t0, report)

    log.append("run_end", run_id=run_id, ok=bool(report["ok"]),
               errors=len(report["errors"]), stages_done=sorted(report["stages"].keys()))
    return report


def _do_mixdown(
    stems_dir: Path,
    mixdown_dir: Path,
    cfg: PipelineConfig,
    stems_pre: list[Path] | None = None,
) -> MixResult:
    """混音内部实现：收集 stems → mixdown。"""
    sources = stems_pre if stems_pre else _gather_stems(stems_dir)
    if not sources:
        raise ValueError("阶段C 无可用音源（请先运行阶段B）。")
    return mixdown(
        sources, mixdown_dir, cfg.mix_config,
        fmt=cfg.mix_fmt,
        separate_mode=cfg.separate_mode,
    )
