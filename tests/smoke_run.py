#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smoke_run.py —— 独立冒烟测试（无需 pytest，用合成小样本跑四阶段 B→C→D）。

用法：
    F:/Python314/python.exe G:/tools/audio_pipeline/tests/smoke_run.py

流程：
    1) 在临时目录建工作区
    2) 种子化 instruments/（合成 WAV stems，模拟阶段A 产物）
    3) run_pipeline(B→C→D) 全链路
    4) 逐项断言产物存在 / sha256 一致
    5) 打印摘要，exit 0（成功）或 1（失败）

不依赖 demucs 下载 / 网络 / 真实歌曲 / pytest。
"""

from __future__ import annotations

import math
import os
import shutil
import struct
import sys
import tempfile
import wave
from pathlib import Path

# 确保项目根在 sys.path
_HERE = Path(__file__).resolve().parent
_PROJ_ROOT = _HERE.parent
sys.path.insert(0, str(_PROJ_ROOT))

from config import bootstrap_env
bootstrap_env()

from config import HONESTY_DISCLAIMER
from instrument_pool import InstrumentPool
from source_maker import TrackSpec
from orchestrator import PipelineConfig, run_pipeline
from manifest import read_manifest, sha256_file


def _make_sine_wav(path: Path, duration_sec: float, freq: float) -> None:
    """生成 16-bit 单声道正弦 WAV。"""
    sr = 44100
    n = int(duration_sec * sr)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        for i in range(n):
            val = int(32767 * 0.5 * math.sin(2 * math.pi * freq * i / sr))
            wf.writeframes(struct.pack("<h", val))


def _seed_instruments(fixtures: Path) -> None:
    """种子化乐器池（模拟阶段A 产物）。"""
    song_dir = fixtures / "test_song"
    sounds = {
        "drums.wav": (200, 0.3),
        "bass.wav": (110, 1.0),
        "piano.wav": (440, 1.5),
        "guitar.wav": (880, 1.0),
        "other.wav": (660, 1.0),
    }
    for name, (freq, dur) in sounds.items():
        _make_sine_wav(song_dir / name, duration_sec=dur, freq=freq)


def main() -> int:
    """冒烟测试主入口，返回 0（成功）或 1（失败）。"""
    errors: list[str] = []
    ws_parent = Path(tempfile.mkdtemp(prefix="smoke_audio_pipeline_"))
    ws = ws_parent / "workspace"
    ws.mkdir(parents=True)

    try:
        # ── 0. 基础检查 ──
        print("═══ audio_pipeline 冒烟测试 ═══")
        print(HONESTY_DISCLAIMER)
        print()

        assert "torch" not in sys.modules, "R1 违反：torch 在编排进程！"
        print("[✓] R1 依赖隔离：torch ∉ sys.modules")

        # ── 1. 种子化乐器池 ──
        fixtures = ws / "instruments"
        _seed_instruments(fixtures)
        pool = InstrumentPool.load(fixtures)
        n = sum(len(v) for v in pool.samples_by_key.values())
        assert n >= 5, f"种子化失败，仅有 {n} 个样本"
        print(f"[✓] 乐器池：{n} 个样本（{len(pool.samples_by_key)} 族）")

        # ── 2. B→C→D ──
        cfg = PipelineConfig(
            workspace=ws,
            songs=[],
            stages=["B", "C", "D"],
            separate_mode="manifest",
            track_specs=[
                TrackSpec(name="bass", kind="synth", bpm=120, duration_sec=2.0, fmt="wav"),
                TrackSpec(name="pad", kind="synth", bpm=120, duration_sec=2.0, fmt="wav"),
            ],
            mix_fmt="wav",
        )
        print(f"启动流水线 B→C→D，工作区: {ws}")
        report = run_pipeline(cfg)

        assert report["ok"], f"流水线失败: {report['errors']}"
        print(f"  B: {report['stages'].get('B', '-')}")
        print(f"  C: {report['stages'].get('C', '-')}")
        print(f"  D: {report['stages'].get('D', '-')}")

        # ── 3. 产物校验 ──
        stems_dir = ws / "stems"
        stems = sorted(stems_dir.glob("*.wav"))
        assert len(stems) == 2, f"阶段B 应为 2 条 stems，实际 {len(stems)}"
        print(f"[✓] 阶段B：{len(stems)} 条 stems")

        song = ws / "mixdown" / "song_final.wav"
        assert song.is_file(), "阶段C 缺 song_final.wav"
        print(f"[✓] 阶段C：{song.name}")

        manifest_path = ws / "mixdown" / "manifest.json"
        assert manifest_path.is_file()
        mani = read_manifest(manifest_path)
        assert len(mani.sources) == 2
        print(f"[✓] manifest：{len(mani.sources)} 条 source")

        sources_dir = ws / "mixdown" / "_sources"
        separated_dir = ws / "separated"
        for e in mani.sources:
            orig = sources_dir / e.output_name
            sep = separated_dir / e.output_name
            assert orig.is_file(), f"_sources 缺 {e.output_name}"
            assert sep.is_file(), f"separated 缺 {e.output_name}"
            h_orig = sha256_file(orig)
            h_sep = sha256_file(sep)
            assert h_orig == h_sep, (
                f"模式A 保真失败：{e.output_name}\n"
                f"  _sources: {h_orig}\n"
                f"  separated: {h_sep}"
            )
        print("[✓] 模式A 保真：逐字节 sha256 一致（100% 无损）")

        # ── 4. 断点续跑 ──
        report2 = run_pipeline(cfg)
        assert report2["ok"]
        for stage in ("B", "C", "D"):
            status = report2["stages"].get(stage, "")
            assert "skip" in status.lower() or "already" in status.lower(), (
                f"断点续跑失败：阶段 {stage} 未跳过 — '{status}'"
            )
        print("[✓] 断点续跑：全部阶段正确跳过")

        print()
        print("═══ 冒烟测试通过 ✓ ═══")
        return 0

    except Exception as exc:
        print(f"\n✗ 冒烟测试失败: {exc}")
        import traceback
        traceback.print_exc()
        return 1

    finally:
        # 清理临时目录
        shutil.rmtree(ws_parent, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
