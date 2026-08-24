# -*- coding: utf-8 -*-
"""
instrument_pool.py —— 阶段 A：乐器池制作（T2）

职责：
  - STEM_TO_POOL_KEY：demucs stem 名 → 乐器族映射（决策④）
  - PoolSample：单条池样本（乐器族 / 路径 / 延迟加载的 Sample）
  - InstrumentPool：按乐器族归类的样本集合，支持 get_pad() 挂载采样鼓垫
  - build_instrument_pool()：阶段A 主入口
      * 对每个 song 通过 audio_separator 子进程拆 stems（WAV，恒 .wav）到
        out_dir/<song>/<stem>.wav（满足 P0-1 采样器仅吃 .wav）
      * 若 out_dir/<song>/ 已存在分轨（合成测试桩 / 断点续跑），直接复用，不触发 demucs
      * 按 STEM_TO_POOL_KEY 归类，返回 InstrumentPool

依赖隔离：audio_separator 仅 subprocess 调用（torch 不进本进程）。
设计依据：docs/design.md §2.2、§6.1、§6.2。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from config import (
    bootstrap_env,
    build_separator_command,
    run_separator,
    DEFAULT_SEP_MODEL,
    DEFAULT_SEP_SHIFTS,
)
from bloodmusic.music.sampler import Sample, SamplePad, load_sample  # 调用，不改

bootstrap_env()

# 决策④：stem 名 → 采样器 SamplePad key（乐器族映射）
STEM_TO_POOL_KEY: dict[str, str] = {
    "drums": "drums",
    "bass": "bass",
    "piano": "piano",
    "guitar": "guitar",
    "other": "other",     # 非人声伴奏残留（htdemucs 四轨的 other）
    "vocals": "_ignore_",  # 纯音乐流程忽略/单独标注
}

# 仅纳入乐器池的 stem（vocals 已被映射为 _ignore_）
POOL_STEMS = [k for k, v in STEM_TO_POOL_KEY.items() if v != "_ignore_"]


@dataclass
class PoolSample:
    """乐器池中的单条样本。"""

    key: str                 # 乐器族，如 "drums"
    path: Path               # instruments/<song>/<stem>.wav
    pad_key: str = ""        # 挂载到 SamplePad 的槽位键（同名多样本轮替时加索引）
    sample: Optional[Sample] = None  # 延迟加载的 bloodmusic Sample（仅 .wav）

    def ensure_sample(self) -> Sample:
        """延迟加载 Sample（仅首次调用时真正读盘）。"""
        if self.sample is None:
            self.sample = load_sample(str(self.path), name=self.pad_key, root_note="C4")
        return self.sample


@dataclass
class InstrumentPool:
    """按乐器族归类的乐器池。"""

    root: Path
    samples_by_key: dict[str, list[PoolSample]] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path) -> "InstrumentPool":
        """从磁盘目录扫描乐器池。

        目录结构：root/<song>/<stem>.wav，仅纳入 POOL_STEMS 中的 stem。

        Args:
            root: 乐器池根目录（如 instruments/）。

        Returns:
            InstrumentPool。
        """
        root = Path(root)
        samples_by_key: dict[str, list[PoolSample]] = {k: [] for k in POOL_STEMS}
        if root.is_dir():
            for song_dir in sorted(p for p in root.iterdir() if p.is_dir()):
                for wav in sorted(song_dir.glob("*.wav")):
                    stem_name = wav.stem
                    if stem_name not in STEM_TO_POOL_KEY:
                        continue
                    key = STEM_TO_POOL_KEY[stem_name]
                    if key == "_ignore_":
                        continue
                    samples_by_key.setdefault(key, []).append(
                        PoolSample(key=key, path=wav)
                    )
        # 给同名多样本分配 pad_key（单样本直接用乐器族名，多样本加索引便于轮替）
        for key, samples in samples_by_key.items():
            for i, ps in enumerate(samples):
                ps.pad_key = key if len(samples) == 1 else f"{key}_{i}"
        return cls(root=root, samples_by_key=samples_by_key)

    def get_pad(self, keys: Optional[list[str]] = None) -> SamplePad:
        """按乐器族挂载 SamplePad。

        Args:
            keys: 需要的乐器族列表；None 表示全部。

        Returns:
            SamplePad，槽位键为 PoolSample.pad_key。

        Raises:
            ValueError: 指定乐器族无任何样本。
        """
        pad = SamplePad()
        selected: list[PoolSample] = []
        for k in (keys or list(self.samples_by_key.keys())):
            selected.extend(self.samples_by_key.get(k, []))
        if not selected:
            wanted = ", ".join(keys) if keys else "全部"
            raise ValueError(f"乐器池缺少样本（请求乐器族：{wanted}）。")
        for ps in selected:
            pad.add(ps.pad_key, ps.ensure_sample(), volume=1.0)
        return pad

    def has(self, key: str) -> bool:
        """乐器族是否有样本。"""
        return bool(self.samples_by_key.get(key))


def _discover_existing_stems(target_dir: Path) -> dict[str, Path]:
    """扫描目标目录里已存在的乐器池 stem（用于合成测试桩 / 断点续跑复用）。"""
    found: dict[str, Path] = {}
    if not target_dir.is_dir():
        return found
    for wav in target_dir.glob("*.wav"):
        stem_name = wav.stem
        if stem_name in STEM_TO_POOL_KEY and STEM_TO_POOL_KEY[stem_name] != "_ignore_":
            found[stem_name] = wav
    return found


def build_instrument_pool(
    songs: list[Path],
    out_dir: Path,
    *,
    model: str = DEFAULT_SEP_MODEL,
    shifts: int = DEFAULT_SEP_SHIFTS,
) -> InstrumentPool:
    """阶段A 主入口：拆歌 → 乐器池（subprocess audio_separator，恒输出 WAV）。

    流程（每个 song）：
      1) 若 out_dir/<song_stem>/ 已存在分轨 → 直接复用（合成测试桩 / 断点续跑，不触发 demucs）；
      2) 否则子进程调用 demucs 全 stem 分离（不加 --two-stems vocals），
         产物在 out_dir/<model>/<song_stem>/*.wav，搬运到 out_dir/<song_stem>/<stem>.wav；
      3) 全部处理完后从 out_dir 扫描构建 InstrumentPool。

    Args:
        songs: 输入歌曲路径列表（可为空 → 仅从已存在目录加载）。
        out_dir: 乐器池输出根目录（如 instruments/）。
        model: 分离模型（默认 htdemucs）。
        shifts: demucs shifts。

    Returns:
        InstrumentPool。

    Raises:
        RuntimeError: demucs 子进程非零退出（含首包模型下载失败等）。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for song in songs:
        song = Path(song)
        if not song.is_file():
            raise FileNotFoundError(f"阶段A 输入歌曲不存在: {song}")
        song_stem = song.stem
        target_dir = out_dir / song_stem

        # 1) 已存在分轨 → 复用（合成测试桩 / 断点续跑）
        existing = _discover_existing_stems(target_dir)
        if existing:
            continue

        # 2) 子进程 demucs 全 stem 分离
        cmd = build_separator_command(song, out_dir, model, shifts)
        ret, out_text = run_separator(cmd)
        if ret != 0:
            raise RuntimeError(
                f"demucs 分离失败（song={song.name}, model={model}, code={ret}）。\n"
                f"--- demucs 输出 ---\n{out_text[-2000:]}\n"
                "（首次运行需联网下载模型权重；决策⑥允许，之后离线。）"
            )

        # 3) 搬运 demucs 产物到 out_dir/<song_stem>/<stem>.wav
        #    demucs 输出结构：out_dir/<model>/<song_stem>/*.wav
        produced_dir = out_dir / model / song_stem
        if not produced_dir.is_dir():
            # 某些 demucs 版本/参数下路径可能不含 model 层，兜底尝试
            produced_dir = out_dir / song_stem
        if not produced_dir.is_dir():
            raise RuntimeError(
                f"demucs 未产出预期目录：{out_dir / model / song_stem}"
            )
        target_dir.mkdir(parents=True, exist_ok=True)
        for wav in produced_dir.glob("*.wav"):
            shutil.copyfile(wav, target_dir / wav.name)

    # 最终从 out_dir 扫描构建乐器池
    return InstrumentPool.load(out_dir)
