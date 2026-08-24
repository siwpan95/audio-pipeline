# -*- coding: utf-8 -*-
"""
separate_back.py —— 阶段 D：后期重分离（T5）

职责：
  - SeparateMode：模式A（manifest，真无损）| 模式B（algo，有损）
  - separate_back()：阶段D 主入口
      * 模式A(manifest)：读 manifest → 把 _sources/ 原文件 100% 保真复制到 separated/；
                          逐字节 sha256 校验一致（真无损断言）；标注「真无损」。
      * 模式B(algo)：subprocess 调 audio_separator 对成品歌重分离；
                      首行打印 HONESTY_DISCLAIMER（有损近似）；
                      输出 separated/<model>/<song>/<stem>.wav。

诚实性（决策⑦ / P0-7）：模式B 必须明确标注有损，绝不宣称无损还原。
依赖隔离：模式B 仅 subprocess 调 audio_separator（torch 留 venv）。
设计依据：docs/design.md §2.5、§6.1、§6.3。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from config import (
    bootstrap_env,
    build_separator_command,
    run_separator,
    DEFAULT_SEP_MODEL,
    DEFAULT_SEP_SHIFTS,
    HONESTY_DISCLAIMER,
    HONESTY_MODE_A,
    HONESTY_MODE_B,
)
from manifest import read_manifest, sha256_file

bootstrap_env()

SeparateMode = Literal["manifest", "algo"]


def separate_back(
    song: Path,
    out_dir: Path,
    mode: SeparateMode = "manifest",
    manifest: Path | None = None,
    model: str = DEFAULT_SEP_MODEL,
    shifts: int = DEFAULT_SEP_SHIFTS,
) -> dict:
    """阶段D 主入口：后期重分离。

    Args:
        song: 成品歌路径（模式B 重分离对象；模式A 仅作占位/校验参考）。
        out_dir: 分离产物输出目录（如 separated/）。
        mode: "manifest"(A，默认) | "algo"(B)。
        manifest: 模式A 所需的 manifest.json 路径（含 _sources 引用与哈希）。
        model: 模式B 分离模型。
        shifts: 模式B shifts。

    Returns:
        结果字典（mode / files / note / fidelity 等）。

    Raises:
        ValueError: 模式A 缺少 manifest / 校验失败；模式B 子进程失败。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if mode == "manifest":
        return _separate_back_manifest(song, out_dir, manifest)
    return _separate_back_algo(song, out_dir, model, shifts)


def _separate_back_manifest(song: Path, out_dir: Path, manifest: Path | None) -> dict:
    """模式A：复用混音时保留的源分轨，100% 保真复制 + sha256 校验。"""
    if manifest is None or not Path(manifest).is_file():
        raise ValueError("模式A(manifest) 必须提供 manifest.json 路径。")

    mani = read_manifest(manifest)
    sources_dir = Path(manifest).parent / "_sources"
    if not sources_dir.is_dir():
        raise ValueError(f"模式A 找不到 _sources 目录：{sources_dir}")

    copied: list[str] = []
    for entry in mani.sources:
        src_file = sources_dir / entry.output_name
        if not src_file.is_file():
            raise ValueError(f"模式A 源分轨缺失：{src_file}（manifest 引用不一致）。")
        dest = out_dir / entry.output_name
        # 硬拷贝（逐字节一致）
        with src_file.open("rb") as fsrc, dest.open("wb") as fdst:
            fdst.write(fsrc.read())
        # 100% 保真断言：复制产物哈希 == manifest 记录哈希
        actual = sha256_file(dest)
        if actual != entry.sha256:
            raise ValueError(
                f"模式A 保真校验失败：{entry.output_name}\n"
                f"  manifest: {entry.sha256}\n"
                f"  actual  : {actual}\n"
                "（混音时保留的源分轨被意外改动，无法 100% 还原。）"
            )
        copied.append(str(dest))

    print(f"[模式A] {HONESTY_MODE_A}")
    print(f"[模式A] 已从 _sources 还原 {len(copied)} 条分轨（逐字节 sha256 一致）。")
    return {
        "mode": "manifest",
        "fidelity": "lossless (manifest copy, byte-identical)",
        "separated_dir": str(out_dir),
        "files": copied,
        "note": HONESTY_MODE_A,
    }


def _separate_back_algo(song: Path, out_dir: Path, model: str, shifts: int) -> dict:
    """模式B：对成品歌算法重分离（有损近似，明确标注）。"""
    song = Path(song)
    if not song.is_file():
        raise FileNotFoundError(f"模式B 成品歌不存在: {song}")

    # 首行打印诚实性声明（有损近似）—— CLI/日志统一标注（P0-7 / 决策⑦）
    print(HONESTY_DISCLAIMER)

    cmd = build_separator_command(song, out_dir, model, shifts)
    ret, out_text = run_separator(cmd)
    if ret != 0:
        raise RuntimeError(
            f"模式B 算法重分离失败（song={song.name}, model={model}, code={ret}）。\n"
            f"--- demucs 输出 ---\n{out_text[-2000:]}\n"
            "（首次运行需联网下载模型权重；决策⑥允许，之后离线。）"
        )

    # 输出结构：out_dir/<model>/<song_stem>/*.wav
    produced_dir = out_dir / model / song.stem
    if not produced_dir.is_dir():
        produced_dir = out_dir / song.stem
    files = [str(p) for p in sorted(produced_dir.glob("*.wav"))] if produced_dir.is_dir() else []

    print(f"[模式B] {HONESTY_MODE_B}")
    print(f"[模式B] 算法重分离完成，产出 {len(files)} 条 stems（有损近似，非无损还原）。")
    return {
        "mode": "algo",
        "fidelity": "lossy (algorithmic separation, SDR≈9–12dB)",
        "model": model,
        "separated_dir": str(produced_dir),
        "files": files,
        "note": HONESTY_MODE_B,
    }
