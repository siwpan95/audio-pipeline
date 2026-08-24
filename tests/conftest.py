# -*- coding: utf-8 -*-
"""
conftest.py —— pytest fixtures（合成小样本、隔离探针、临时工作区）

合成策略（不依赖 demucs 下载 / 真实歌曲）：
  - 用标准库 wave 生成几秒正弦 .wav 作为「歌曲」和「乐器池 stems」
  - bloodmusic load_sample() 直接可读（16-bit PCM WAV）
  - 所有 fixture 基于 tmp_path，测试结束自动清理

注意：不使用 @pytest.fixture 的 autouse 可能导致 import 触发副作用，
      但本文件仅定义数据桩，不做全局 import。
"""

from __future__ import annotations

import math
import os
import shutil
import struct
import sys
import wave
from pathlib import Path

import pytest

# 确保 test 进程能 import 扁平米模块（cli.py 同级目录）
_HERE = Path(__file__).resolve().parent
_PROJ_ROOT = _HERE.parent
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))


# ═══════════════════════════ 工具函数 ═══════════════════════════


def make_sine_wav(
    path: Path,
    duration_sec: float = 1.0,
    sr: int = 44100,
    freq: float = 440.0,
    amplitude: float = 0.5,
    channels: int = 1,
) -> Path:
    """生成正弦波 16-bit PCM WAV 文件（load_sample 可加载）。

    Args:
        path: 输出路径。
        duration_sec: 时长（秒）。
        sr: 采样率。
        freq: 频率（Hz）。
        amplitude: 幅度 0.0-1.0。
        channels: 声道数。

    Returns:
        实际写入路径。
    """
    n_samples = int(duration_sec * sr)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # 16-bit signed
        wf.setframerate(sr)
        for i in range(n_samples):
            val = int(32767 * amplitude * math.sin(2 * math.pi * freq * i / sr))
            wf.writeframes(struct.pack("<h", val))
    return path


def make_drum_click_wav(
    path: Path,
    duration_sec: float = 0.5,
    sr: int = 44100,
) -> Path:
    """生成短促的「鼓击」WAV（衰减正弦，模拟打击感）。"""
    n_samples = int(duration_sec * sr)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        for i in range(n_samples):
            t = i / sr
            env = math.exp(-t * 15)  # 快速衰减
            val = int(32767 * 0.8 * env * math.sin(2 * math.pi * 200 * t))
            wf.writeframes(struct.pack("<h", val))
    return path


# ═══════════════════════════ fixtures ═══════════════════════════


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """临时工作区（含 instruments/stems/mixdown/separated 子目录）。"""
    ws = tmp_path / "audio_pipeline_workspace"
    for d in ("instruments", "stems", "mixdown", "mixdown/_sources", "separated"):
        (ws / d).mkdir(parents=True, exist_ok=True)
    return ws


@pytest.fixture
def synthetic_song(tmp_path: Path) -> Path:
    """一段几秒合成正弦 WAV（模拟「真实歌曲」喂给乐器池/混音）。"""
    return make_sine_wav(tmp_path / "song.wav", duration_sec=2.0, freq=330.0)


@pytest.fixture
def seeded_instruments(tmp_workspace: Path) -> Path:
    """为测试预先在 instruments/ 下生成「假」stem WAV（模拟阶段A 产物）。

    目录结构（符合 §6.1）：
      instruments/
        test_song/
          drums.wav（鼓点击，~0.5s）
          bass.wav（正弦 110Hz）
          piano.wav（正弦 440Hz）
          guitar.wav（正弦 880Hz）
          other.wav（白噪替代 — 用不同频率正弦）
    """
    fixtures = tmp_workspace / "instruments"
    song_dir = fixtures / "test_song"
    song_dir.mkdir(parents=True, exist_ok=True)
    make_drum_click_wav(song_dir / "drums.wav")
    make_sine_wav(song_dir / "bass.wav", duration_sec=1.0, freq=110.0)
    make_sine_wav(song_dir / "piano.wav", duration_sec=1.5, freq=440.0)
    make_sine_wav(song_dir / "guitar.wav", duration_sec=1.0, freq=880.0)
    make_sine_wav(song_dir / "other.wav", duration_sec=1.0, freq=660.0)
    return fixtures


@pytest.fixture
def synthetic_midi(tmp_path: Path) -> Path:
    """生成一段最小 MIDI（C4-E4-G4 八分音符，供 bridge 测试）。

    注意：需要 mido 已安装（bloodmusic 依赖它）。
    """
    try:
        import mido
    except ImportError:  # pragma: no cover
        pytest.skip("mido 未安装，跳过 MIDI 导入测试")

    mid = mido.MidiFile()
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(120)))
    notes = [60, 64, 67, 72]  # C4, E4, G4, C5
    tick = 0
    step = 480  # quarter note
    for note in notes:
        track.append(mido.Message("note_on", note=note, velocity=80, time=tick))
        track.append(mido.Message("note_off", note=note, velocity=0, time=step))
        tick = 0
    out = tmp_path / "demo.mid"
    mid.save(str(out))
    return out


# ═══════════════════════════ 全局环境引导 ═══════════════════════════

# 在 pytest 收集阶段即调用 bootstrap_env，确保所有后续模块 import
# 时 remix PATH 已注入、pydub 已 pin、bloodmusic 已验证。
# 幂等调用，多次执行安全。

from config import bootstrap_env
bootstrap_env()


def pytest_configure(config: pytest.Config) -> None:
    """注册自定义标记。"""
    config.addinivalue_line(
        "markers", "slow: 需要真实 demucs（含首次模型下载），默认跳过"
    )
