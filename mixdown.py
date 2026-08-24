# -*- coding: utf-8 -*-
"""
mixdown.py —— 阶段 C：混音成完整歌曲 + 分轨保留（T4）

职责：
  - MixResult：混音产物 / _sources 目录 / manifest 路径
  - mixdown()：用 remix.AudioMixer 混音为母带，并保留原始分轨不可变拷贝 + 写清单

流程（严格对应 design.md §2.4 / 时序图）：
  1) AudioLoader.load_batch(sources)
  2) AudioLoader.unify(16-bit)  —— 统一为 16-bit int（P2-4 受控量化，非无损声明）
  3) AudioLoader.extract_samples → list[list[int]]
  4) AudioMixer.mix_to_segment(arrays, sample_width=2, frame_rate, channels, config) → master
  5) AudioLoader.export(master, song_final)  —— 混音产物（不可逆）
  6) 拷 sources → _sources/（模式A 保留，硬拷贝，不可变）
  7) 写 manifest.json（输出名→乐器类别→原路径→sha256）

依赖隔离：仅在编排进程内 import remix（同 env，torch 不在此 env）。
设计依据：docs/design.md §2.4、§6.1、§6.3。
"""

from __future__ import annotations

import datetime
import shutil
from dataclasses import dataclass
from pathlib import Path

from config import bootstrap_env, HONESTY_DISCLAIMER
from core.audio_mixer import AudioMixer, MixConfig
from core.audio_loader import AudioLoader, AudioParams
from manifest import Manifest, SourceManifestEntry, sha256_file

bootstrap_env()


@dataclass
class MixResult:
    """阶段C 产物引用。"""

    song_path: Path        # mixdown/song_final.<fmt>
    sources_dir: Path      # mixdown/_sources/（模式A 还原源）
    manifest_path: Path    # manifest.json


def _instrument_class_from_name(filename: str) -> str:
    """从产物文件名推断乐器类别（§6.2：<instrument_class>__<track_name>）。"""
    stem = Path(filename).stem
    if "__" in stem:
        return stem.split("__", 1)[0]
    return "synth"


def mixdown(
    sources: list[Path],
    out_dir: Path,
    config: MixConfig,
    *,
    fmt: str = "wav",
    separate_mode: str = "manifest",
    bpm: int | None = None,
    musical_key: str | None = None,
) -> MixResult:
    """阶段C 主入口：混音 + 分轨保留 + 写清单。

    Args:
        sources: 阶段B 产出的单音源文件列表。
        out_dir: 混音输出目录（如 mixdown/）。
        config: MixConfig（增益/归一化/削波/淡变/压缩/滤波透传）。
        fmt: 成品歌格式（默认 wav）。
        separate_mode: 配置的模式（"manifest" | "algo"），写入 manifest。
        bpm: 曲速（写入 manifest，可选）。
        musical_key: 调性（写入 manifest，可选）。

    Returns:
        MixResult。

    Raises:
        ValueError: 无输入源或格式不支持。
    """
    if not sources:
        raise ValueError("mixdown 需要至少一个音源文件。")
    fmt = fmt.lower()
    if fmt not in ("wav", "ogg", "flac"):
        raise ValueError(f"不支持的混音输出格式: {fmt}（应为 wav|ogg|flac）")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1) 批量加载
    loader = AudioLoader()
    segments = loader.load_batch([str(p) for p in sources])
    if not segments:
        raise ValueError("未能加载任何音源。")

    # 2) 统一参数：以首轨为准取 frame_rate / channels，强制 16-bit
    common = loader.detect_common_params(segments)
    target = AudioParams(
        frame_rate=common.frame_rate,
        channels=common.channels,
        sample_width=2,  # 16-bit int，满足 AudioMixer 输入契约
    )
    unified = loader.unify(segments, target)

    # 3) 提取 16-bit int 样本
    sample_arrays = loader.extract_samples(unified)
    sr = common.frame_rate
    ch = common.channels

    # 4) 混音（手动引擎 → AudioSegment）
    master = AudioMixer.mix_to_segment(
        sample_arrays,
        sample_width=2,
        frame_rate=sr,
        channels=ch,
        config=config,
    )

    # 5) 导出母带（不可逆）
    song_path = out_dir / f"song_final.{fmt}"
    loader.export(master, str(song_path), fmt=fmt)

    # 6) 拷贝原始分轨到 _sources/（模式A 保留，硬拷贝，不可变）
    sources_dir = out_dir / "_sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    entries: list[SourceManifestEntry] = []
    for src in sources:
        src = Path(src)
        dest = sources_dir / src.name
        shutil.copyfile(src, dest)  # 硬拷贝，保证不可变、零依赖原 stems
        entries.append(
            SourceManifestEntry(
                output_name=src.name,
                instrument_class=_instrument_class_from_name(src.name),
                original_path=str(src),
                sha256=sha256_file(dest),
            )
        )

    # 7) 写 manifest.json
    manifest = Manifest(
        version="1.0",
        created_at=datetime.datetime.now().isoformat(timespec="seconds"),
        song=song_path.name,
        bpm=bpm,
        musical_key=musical_key,
        sample_rate=int(sr),
        bit_depth=16,
        sources=entries,
        separate_mode=separate_mode,
        note=HONESTY_DISCLAIMER,
    )
    manifest_path = out_dir / "manifest.json"
    from manifest import write_manifest
    write_manifest(manifest_path, manifest)

    return MixResult(
        song_path=song_path,
        sources_dir=sources_dir,
        manifest_path=manifest_path,
    )
