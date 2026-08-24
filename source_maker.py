# -*- coding: utf-8 -*-
"""
source_maker.py —— 阶段 B：单音源导出（T3）

职责：
  - TrackSpec：单条音源规格（合成 / 采样两种来源）
  - make_source()：导出单条音源分轨到 stems/，返回产物路径
      * kind=="synth"  → bridge 振荡器合成（优先路径，决策⑤）
      * kind=="sampled"→ pool.get_pad + render_sampler_pattern（消费阶段A乐器池，落实 G1）

零改造：仅调用 bloodmusic.bridge / music.sampler / export.batch，不修改其源码（R4）。
设计依据：docs/design.md §2.3、§6.2、§7。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from config import bootstrap_env
from bloodmusic.bridge import (
    midi_to_patterns_v2,
    render_pattern_file,
    BridgeConfig,
)
from bloodmusic.bridge.models import ImportResult
from bloodmusic.music.pattern import MusicPattern, DrumPattern
from bloodmusic.music.sampler import SamplePad, render_sampler_pattern
from bloodmusic.export.batch import export_audio
from instrument_pool import InstrumentPool

bootstrap_env()


@dataclass
class TrackSpec:
    """单条音源规格。"""

    name: str                          # 音源名（用于产物文件名）
    kind: str = "synth"                # "synth"(bridge 合成) | "sampled"(采样器消费乐器池)
    pattern_source: Optional[Path] = None   # MIDI 资产（midi_to_patterns_v2 输入）
    pattern_name: Optional[str] = None      # 从 ImportResult 选单个 pattern 的名称
    pool_key: Optional[str] = None          # sampled: 乐器族（drums/bass/piano/guitar/other）
    sample_map: Optional[dict] = None       # sampled: 音符名 → pad key
    fmt: str = "wav"                  # wav|ogg|flac（P0-2）
    duration_sec: Optional[float] = None
    bpm: Optional[int] = None


def _programmatic_pattern(spec: TrackSpec) -> MusicPattern:
    """无 MIDI 时的内置程序化 pattern（决策：允许无 MIDI 程序化 pattern）。"""
    bpm = int(spec.bpm) if spec.bpm else 120
    return MusicPattern(
        name=spec.name,
        notes=["C4", "E4", "G4", "C5"],
        durations=[1.0, 1.0, 1.0, 1.0],
        bpm=bpm,
        loop=False,
        volume=0.6,
    )


def _resolve_pattern(spec: TrackSpec, require_music: bool) -> object:
    """解析出要渲染的 pattern。

    Args:
        spec: 音源规格。
        require_music: True 表示必须是 MusicPattern（sampled 路径）。

    Returns:
        MusicPattern 或 DrumPattern。

    Raises:
        ValueError: 无法解析出可用 pattern。
    """
    pat = None
    src = Path(spec.pattern_source) if spec.pattern_source else None
    if src is not None and src.is_file():
        result: ImportResult = midi_to_patterns_v2(str(src), bpm=spec.bpm)
        candidates = result.all_patterns  # 旋律 + 和弦 + 鼓
        # sampled 路径只要 MusicPattern（render_sampler_pattern 需要 notes/durations）
        if require_music:
            candidates = result.all_music_patterns
        if spec.pattern_name:
            pat = next((p for p in candidates if p.name == spec.pattern_name), None)
        else:
            pat = candidates[0] if candidates else None

    if pat is None:
        # 退化为程序化 pattern（始终为 MusicPattern，满足 sampled 需求）
        pat = _programmatic_pattern(spec)
    return pat


def _natural_duration(pat: object) -> float:
    """按 pattern 自然时长反推秒数（D6：不用拍脑袋时长）。"""
    bpm = getattr(pat, "bpm", 120) or 120
    durations = getattr(pat, "durations", []) or []
    total_beats = sum(float(d) for d in durations)
    if total_beats <= 0:
        return 2.0
    return max(0.05, total_beats * 60.0 / float(bpm))


def _output_path(out_dir: Path, spec: TrackSpec) -> Path:
    """按 §6.2 命名：stems/<instrument_class>__<track_name>.<fmt>。"""
    instrument_class = spec.pool_key if spec.kind == "sampled" and spec.pool_key else "synth"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{instrument_class}__{spec.name}.{spec.fmt}"


def make_source(track_spec: TrackSpec, pool: InstrumentPool, out_dir: Path) -> Path:
    """阶段B 主入口：导出单条音源分轨。

    Args:
        track_spec: 音源规格。
        pool: 阶段A 乐器池（sampled 路径消费它）。
        out_dir: 输出目录（如 stems/）。

    Returns:
        产物文件路径。

    Raises:
        ValueError: 规格非法（如 sampled 但缺少乐器池样本）。
        BridgeRenderError: bridge 渲染失败（透传）。
    """
    if track_spec.kind not in ("synth", "sampled"):
        raise ValueError(f"未知 kind: {track_spec.kind}（应为 synth | sampled）")

    out_path = _output_path(out_dir, track_spec)
    fmt = track_spec.fmt.lower()
    if fmt not in ("wav", "ogg", "flac"):
        raise ValueError(f"不支持的输出格式: {fmt}（应为 wav|ogg|flac）")

    # ── 解析 pattern ──
    pat = _resolve_pattern(track_spec, require_music=(track_spec.kind == "sampled"))

    # ── 时长 ──
    duration = float(track_spec.duration_sec) if track_spec.duration_sec else _natural_duration(pat)

    if track_spec.kind == "synth":
        # 优先路径：bridge 振荡器合成（零改造复用本体）
        cfg = BridgeConfig(audio_format=fmt, readonly_roots=())
        render_pattern_file(pat, out_path, duration, cfg=cfg)
        return out_path

    # ── sampled：消费阶段A乐器池（落实 G1） ──
    if not track_spec.pool_key:
        raise ValueError("sampled 路径必须指定 pool_key（乐器族）。")
    if not pool.has(track_spec.pool_key):
        raise ValueError(
            f"乐器池缺少 '{track_spec.pool_key}' 样本，"
            f"可用乐器族：{list(pool.samples_by_key.keys())}。"
        )

    pad: SamplePad = pool.get_pad([track_spec.pool_key])
    pad_keys = list(pad.keys)

    # 构建 sample_map：音符名 → pad key（默认轮替多个样本）
    if track_spec.sample_map:
        sample_map = dict(track_spec.sample_map)
    else:
        notes = [n for n in getattr(pat, "notes", []) if n]
        sample_map = {}
        if pad_keys:
            for i, n in enumerate(notes):
                sample_map[n] = pad_keys[i % len(pad_keys)]

    # render_sampler_pattern 需要 MusicPattern；DrumPattern 退化为程序化
    if isinstance(pat, DrumPattern):
        pat = _programmatic_pattern(track_spec)

    audio = render_sampler_pattern(
        pad,
        pat,  # type: ignore[arg-type]
        duration,
        sample_map=sample_map,
    )
    # 统一经 bloodmusic.export_audio 落盘（支持 wav/ogg/flac，P0-2）
    export_audio(audio, str(out_path), format=fmt)
    return out_path
