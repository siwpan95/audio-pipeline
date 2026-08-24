# -*- coding: utf-8 -*-
"""
manifest.py —— 产物清单数据结构与读写（T4 / T2 共用）

职责：
  - SourceManifestEntry：单条源分轨记录（输出名→乐器类别→原路径→sha256）
  - Manifest：全局清单（BPM / 调性 / 采样率 / 位深 / 分轨列表 / 分离模式 / 诚实性说明）
  - sha256_file()：文件哈希（模式A 还原校验）
  - write_manifest() / read_manifest()：JSON 持久化

设计依据：docs/design.md §2.4、§6.3。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path


def sha256_file(path: str | Path) -> str:
    """计算文件 sha256（二进制分块读取，避免大文件占满内存）。

    Args:
        path: 文件路径。

    Returns:
        64 位十六进制 sha256 摘要字符串。

    Raises:
        FileNotFoundError: 文件不存在。
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"清单哈希目标不存在: {path}")
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class SourceManifestEntry:
    """单条源分轨记录（模式A 还原依据）。"""

    output_name: str        # _sources 下的文件名（如 drums__verse.wav）
    instrument_class: str   # 乐器类别：drums/bass/piano/guitar/other
    original_path: str      # 阶段B 产物原路径（相对工作区，如 stems/drums__verse.wav）
    sha256: str             # 文件哈希（模式A 还原校验，100% 保真断言）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SourceManifestEntry":
        return cls(
            output_name=str(d["output_name"]),
            instrument_class=str(d["instrument_class"]),
            original_path=str(d["original_path"]),
            sha256=str(d["sha256"]),
        )


@dataclass
class Manifest:
    """全局产物清单。"""

    version: str = "1.0"
    created_at: str = ""
    song: str = ""                       # 成品歌文件名（如 song_final.wav）
    bpm: int | None = None
    musical_key: str | None = None
    sample_rate: int = 44100
    bit_depth: int = 16
    sources: list[SourceManifestEntry] = field(default_factory=list)
    separate_mode: str = "manifest"      # "manifest"(A) | "algo"(B)
    note: str = ""                       # = HONESTY_DISCLAIMER 或细分说明

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "created_at": self.created_at,
            "song": self.song,
            "bpm": self.bpm,
            "musical_key": self.musical_key,
            "sample_rate": self.sample_rate,
            "bit_depth": self.bit_depth,
            "separate_mode": self.separate_mode,
            "note": self.note,
            "sources": [s.to_dict() for s in self.sources],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Manifest":
        return cls(
            version=str(d.get("version", "1.0")),
            created_at=str(d.get("created_at", "")),
            song=str(d.get("song", "")),
            bpm=d.get("bpm"),
            musical_key=d.get("musical_key"),
            sample_rate=int(d.get("sample_rate", 44100)),
            bit_depth=int(d.get("bit_depth", 16)),
            sources=[SourceManifestEntry.from_dict(s) for s in d.get("sources", [])],
            separate_mode=str(d.get("separate_mode", "manifest")),
            note=str(d.get("note", "")),
        )


def write_manifest(path: str | Path, m: Manifest) -> None:
    """将清单写入 JSON 文件（原子写：先写临时再 rename）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(m.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(p)


def read_manifest(path: str | Path) -> Manifest:
    """从 JSON 文件读取清单。"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"清单不存在: {path}")
    data = json.loads(p.read_text(encoding="utf-8"))
    return Manifest.from_dict(data)
