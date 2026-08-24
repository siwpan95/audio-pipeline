# -*- coding: utf-8 -*-
"""
test_pipeline.py —— 四阶段回归测试（T7）

测试覆盖（设计 §8）：
  - 依赖隔离（R1: torch ∉ sys.modules）
  - InstrumentPool 加载 / get_pad 挂载（P0-1）
  - make_source synth + sampled（P0-2 / G1）
  - mixdown + manifest + sha256（P0-3 / P0-4）
  - separate_back 模式A 100% 保真断言
  - 模式B 有损声明（HONESTY_DISCLAIMER 常量 / 首行打印）
  - run_pipeline B→C→D 集成（断点续跑不触发 demucs）
  - 异常路径（缺 manifest / 缺池样本 / 空音源等）

所有测试使用 conftest 的合成小样本（标准库 wave 生成），
不依赖 demucs 模型下载或真实歌曲。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from config import bootstrap_env
bootstrap_env()  # 幂等，确保测试进程内环境就绪

from config import (
    HONESTY_DISCLAIMER,
    HONESTY_MODE_A,
    HONESTY_MODE_B,
)
from instrument_pool import (
    InstrumentPool,
    build_instrument_pool,
    STEM_TO_POOL_KEY,
)
from source_maker import TrackSpec, make_source
from mixdown import mixdown
from manifest import Manifest, SourceManifestEntry, sha256_file, write_manifest, read_manifest
from separate_back import separate_back
from orchestrator import PipelineConfig, run_pipeline
from core.audio_mixer import MixConfig

# 注册 slow 标记（可选长时/联网依赖测试）
pytestmark = None


# ═════════════════════ R1 依赖隔离 ═════════════════════


def test_torch_not_in_sys_modules() -> None:
    """R1：编排进程内 torch 绝不在 sys.modules。"""
    assert "torch" not in sys.modules, (
        "违反 R1 依赖隔离！torch 被意外 import 进编排进程。"
        "audio_separator 仅允许通过 subprocess 调用。"
    )


def test_remix_importable() -> None:
    """R2：remix.core 可 import（sys.path 注入生效）。"""
    from core.audio_mixer import AudioMixer
    from core.audio_loader import AudioLoader
    assert AudioMixer is not None
    assert AudioLoader is not None


def test_bloodmusic_importable() -> None:
    """R4：bloodmusic 可 import（editable install 有效，零改造）。"""
    import bloodmusic.bridge
    from bloodmusic.music.sampler import SamplePad, load_sample
    assert bloodmusic.bridge is not None
    assert load_sample is not None
    assert SamplePad is not None


def test_pydub_pinned() -> None:
    """pydub converter / ffprobe 已 pin 到 remix/bin。"""
    from pydub import AudioSegment
    conv = Path(AudioSegment.converter)
    assert conv.name in ("ffmpeg.exe", "ffmpeg"), f"converter={conv}"
    assert conv.exists(), f"ffmpeg 缺失: {conv}"


# ═════════════════════ T2 乐器池 ═════════════════════


def test_stem_to_pool_key_mapping() -> None:
    """决策④：STEM_TO_POOL_KEY 映射正确，vocals → _ignore_。"""
    assert STEM_TO_POOL_KEY["drums"] == "drums"
    assert STEM_TO_POOL_KEY["vocals"] == "_ignore_"
    for key in ("drums", "bass", "piano", "guitar", "other"):
        assert key in STEM_TO_POOL_KEY, f"缺少 stem 映射: {key}"


def test_instrument_pool_load(seeded_instruments: Path) -> None:
    """InstrumentPool.load 扫描 seeded instruments 并归类。"""
    pool = InstrumentPool.load(seeded_instruments)
    assert pool.root == seeded_instruments
    assert "drums" in pool.samples_by_key
    assert "bass" in pool.samples_by_key
    # vocals mapped to _ignore_ 不应出现在池中
    assert "_ignore_" not in pool.samples_by_key
    n = sum(len(v) for v in pool.samples_by_key.values())
    assert n >= 5


def test_pool_get_pad(seeded_instruments: Path) -> None:
    """get_pad 挂载 SamplePad，load_sample 可加载 WAV（P0-1）。"""
    pool = InstrumentPool.load(seeded_instruments)
    pad = pool.get_pad(["drums"])
    assert "drums" in pad.keys
    # 触发一次 trigger 验证 load_sample 成功（sample 不为 None，返回非空数组）
    audio = pad.trigger("drums")
    assert audio is not None
    assert len(audio) > 0
    # 能量非零（P0-1 断言）
    import numpy as np
    assert np.max(np.abs(audio)) > 0, "sampled 输出能量为零——load_sample 可能失败"


def test_pool_missing_key_raises(seeded_instruments: Path) -> None:
    """请求缺失乐器族应抛出 ValueError。"""
    pool = InstrumentPool.load(seeded_instruments)
    with pytest.raises(ValueError):
        pool.get_pad(["nonexistent"])


def test_load_sample_only_wav(seeded_instruments: Path, tmp_path: Path) -> None:
    """P0-1 硬约束：load_sample 应能加载 .wav 文件。"""
    from bloodmusic.music.sampler import load_sample
    drums = seeded_instruments / "test_song" / "drums.wav"
    sam = load_sample(str(drums))
    assert sam.name is not None
    assert len(sam.data) > 0

    # 非 WAV 文件应报错（wave 模块无法解析）
    fake = tmp_path / "fake.flac"
    fake.write_text("not a wav")
    with pytest.raises(Exception):
        load_sample(str(fake))


# ═════════════════════ T3 单音源导出 ═════════════════════


def test_make_source_synth_programmatic(seeded_instruments: Path, tmp_workspace: Path) -> None:
    """synth 路径：无 MIDI 的程序化 pattern → 振荡器合成 WAV。"""
    pool = InstrumentPool.load(seeded_instruments)
    stems_dir = tmp_workspace / "stems"
    spec = TrackSpec(name="test_synth", kind="synth", bpm=120, duration_sec=2.0, fmt="wav")
    out = make_source(spec, pool, stems_dir)
    assert out.is_file()
    assert out.suffix == ".wav"
    assert out.stat().st_size > 100, "产物过小——渲染可能为空"
    # 文件名符合 §6.2
    assert out.name == "synth__test_synth.wav"


def test_make_source_sampled(seeded_instruments: Path, tmp_workspace: Path) -> None:
    """sampled 路径：消费乐器池 drums → 渲染 WAV（落实 G1）。"""
    pool = InstrumentPool.load(seeded_instruments)
    stems_dir = tmp_workspace / "stems"
    spec = TrackSpec(
        name="kick", kind="sampled", pool_key="drums",
        bpm=120, duration_sec=1.0, fmt="wav",
    )
    out = make_source(spec, pool, stems_dir)
    assert out.is_file()
    assert out.stat().st_size > 100
    # 文件名含乐器族
    assert out.name.startswith("drums__")


def test_make_source_sampled_missing_pool(seeded_instruments: Path, tmp_workspace: Path) -> None:
    """sampled 路径：请求缺失的乐器族应报错。"""
    pool = InstrumentPool.load(seeded_instruments)
    stems_dir = tmp_workspace / "stems"
    spec = TrackSpec(
        name="fail", kind="sampled", pool_key="nonexistent",
        bpm=120, fmt="wav",
    )
    with pytest.raises(ValueError, match="缺少"):
        make_source(spec, pool, stems_dir)


def test_make_source_synth_from_midi(synthetic_midi: Path, seeded_instruments: Path, tmp_workspace: Path) -> None:
    """synth 路径：从 MIDI 文件 import → 渲染。"""
    pool = InstrumentPool.load(seeded_instruments)
    stems_dir = tmp_workspace / "stems"
    spec = TrackSpec(
        name="midi_synth", kind="synth",
        pattern_source=synthetic_midi, bpm=120, fmt="wav",
    )
    out = make_source(spec, pool, stems_dir)
    assert out.is_file()
    assert out.stat().st_size > 100


# ═════════════════════ T4 混音 + manifest ═════════════════════


def test_manifest_roundtrip(tmp_path: Path) -> None:
    """manifest 写 → 读 往返一致。"""
    m = Manifest(
        song="song_final.wav", bpm=120, sample_rate=44100,
        sources=[SourceManifestEntry("a.wav", "drums", "stems/a.wav", "abc123")],
        separate_mode="manifest", note=HONESTY_DISCLAIMER,
    )
    p = tmp_path / "manifest.json"
    write_manifest(p, m)
    m2 = read_manifest(p)
    assert m2.song == m.song
    assert m2.bpm == 120
    assert m2.sources[0].sha256 == "abc123"


def test_sha256_file(tmp_path: Path) -> None:
    """sha256 计算正确。"""
    f = tmp_path / "data.bin"
    f.write_bytes(b"hello world")
    h = sha256_file(f)
    assert len(h) == 64
    assert sha256_file(f) == h  # 幂等


def test_mixdown_with_synth_stems(
    seeded_instruments: Path, tmp_workspace: Path, synthetic_song: Path,
) -> None:
    """阶段B→C 集成：生成 synth stems → 混音 → manifest + _sources。"""
    # 先用 make_source 产几条音源
    pool = InstrumentPool.load(seeded_instruments)
    stems_dir = tmp_workspace / "stems"
    sources: list[Path] = []
    for name, dur in [("bass", 2.0), ("pad", 2.0)]:
        spec = TrackSpec(name=name, kind="synth", bpm=120, duration_sec=dur, fmt="wav")
        sources.append(make_source(spec, pool, stems_dir))

    # 混音
    mixdown_dir = tmp_workspace / "mixdown"
    result = mixdown(sources, mixdown_dir, MixConfig(), fmt="wav", separate_mode="manifest")

    assert result.song_path.is_file()
    assert result.song_path.stat().st_size > 100
    assert result.sources_dir.is_dir()
    assert result.manifest_path.is_file()

    # _sources 含全部源
    for src in sources:
        assert (result.sources_dir / src.name).is_file()

    # manifest 校验
    mani = read_manifest(result.manifest_path)
    assert mani.song == result.song_path.name
    assert mani.bit_depth == 16
    assert mani.separate_mode == "manifest"
    assert len(mani.sources) == len(sources)


# ═════════════════════ T5 模式A 100% 保真 ═════════════════════


def test_separate_back_mode_a_fidelity(
    seeded_instruments: Path, tmp_workspace: Path,
) -> None:
    """模式A 还原断言：separated/ 文件与 _sources/ sha256 完全一致（100% 保真）。"""
    # 先混音（得到 manifest + _sources）
    pool = InstrumentPool.load(seeded_instruments)
    stems_dir = tmp_workspace / "stems"
    source = make_source(
        TrackSpec(name="fid", kind="synth", bpm=120, duration_sec=1.0, fmt="wav"),
        pool, stems_dir,
    )
    mixdown_dir = tmp_workspace / "mixdown"
    mixdown([source], mixdown_dir, MixConfig(), fmt="wav", separate_mode="manifest")

    # 模式A 还原
    separated_dir = tmp_workspace / "separated"
    manifest_path = mixdown_dir / "manifest.json"
    song = mixdown_dir / read_manifest(manifest_path).song
    res = separate_back(song, separated_dir, mode="manifest", manifest=manifest_path)

    assert res["mode"] == "manifest"
    assert res["fidelity"] == "lossless (manifest copy, byte-identical)"
    assert len(res["files"]) == 1

    # 逐字节 sha256 一致（100% 保真 — 设计 §8.3）
    sources_dir = mixdown_dir / "_sources"
    for entry in read_manifest(manifest_path).sources:
        orig_sha = sha256_file(sources_dir / entry.output_name)
        sep_sha = sha256_file(separated_dir / entry.output_name)
        assert orig_sha == sep_sha, (
            f"模式A 保真失败：{entry.output_name} sha256 不一致"
        )
        assert orig_sha == entry.sha256


def test_separate_back_mode_a_missing_manifest(tmp_workspace: Path) -> None:
    """模式A 缺 manifest 应抛出 ValueError。"""
    with pytest.raises(ValueError, match="manifest"):
        separate_back(
            tmp_workspace / "song.wav", tmp_workspace / "separated",
            mode="manifest", manifest=None,
        )


# ═════════════════════ T5 模式B 有损声明 ═════════════════════


def test_honesty_disclaimer_constant() -> None:
    """P0-7：HONESTY_DISCLAIMER 含关键声明。"""
    assert "有损" in HONESTY_DISCLAIMER or "lossy" in HONESTY_DISCLAIMER.lower()
    assert "SDR" in HONESTY_DISCLAIMER
    assert "模式A" in HONESTY_DISCLAIMER or "manifest" in HONESTY_DISCLAIMER.lower()
    assert "模式B" in HONESTY_DISCLAIMER or "algo" in HONESTY_DISCLAIMER.lower()


def test_separate_back_mode_b_missing_file(tmp_workspace: Path) -> None:
    """模式B：成品歌不存在应报 FileNotFoundError（不触发 demucs）。"""
    with pytest.raises(FileNotFoundError):
        separate_back(
            tmp_workspace / "nonexistent.wav",
            tmp_workspace / "separated",
            mode="algo",
        )


# ═════════════════════ T6 全链路集成（B→C→D，种子化乐器池） ═════════════════════


def test_run_pipeline_bcd_integration(
    seeded_instruments: Path, tmp_workspace: Path,
) -> None:
    """run_pipeline B→C→D（模式A）：合成 stems → 混音 → 还原，断点续跑不触发 demucs。"""
    ws = tmp_workspace
    # 乐器池已种子化
    cfg = PipelineConfig(
        workspace=ws,
        songs=[],                     # 空 songs → A 跳过，B 从已有池加载
        stages=["B", "C", "D"],
        separate_mode="manifest",
        track_specs=[
            TrackSpec(name="b1", kind="synth", bpm=120, duration_sec=1.5, fmt="wav"),
            TrackSpec(name="p1", kind="synth", bpm=120, duration_sec=1.5, fmt="wav"),
        ],
        mix_fmt="wav",
    )
    report = run_pipeline(cfg)
    assert report["ok"], f"流水线失败: {report['errors']}"
    assert "B" in report["stages"]
    assert "C" in report["stages"]
    assert "D" in report["stages"]

    # 产物文件存在
    assert (ws / "stems").is_dir()
    assert any((ws / "stems").glob("*.wav"))
    assert (ws / "mixdown" / "song_final.wav").is_file()
    manifest_path = ws / "mixdown" / "manifest.json"
    assert manifest_path.is_file()

    # 模式A 还原文件与 _sources 完全一致（逐字节）
    sources_dir = ws / "mixdown" / "_sources"
    separated_dir = ws / "separated"
    mani = read_manifest(manifest_path)
    for e in mani.sources:
        orig = sources_dir / e.output_name
        sep = separated_dir / e.output_name
        assert orig.is_file()
        assert sep.is_file()
        assert sha256_file(orig) == sha256_file(sep)


def test_run_pipeline_resume(
    seeded_instruments: Path, tmp_workspace: Path,
) -> None:
    """断点续跑：首次运行 B→C→D 后第二次运行应全部 skip。"""
    ws = tmp_workspace
    cfg = PipelineConfig(
        workspace=ws,
        songs=[],
        stages=["B", "C", "D"],
        separate_mode="manifest",
        track_specs=[
            TrackSpec(name="r1", kind="synth", bpm=120, duration_sec=1.0, fmt="wav"),
        ],
        mix_fmt="wav",
    )
    report1 = run_pipeline(cfg)
    assert report1["ok"]

    # 第二次运行
    report2 = run_pipeline(cfg)
    assert report2["ok"]
    # 各阶段应标记为 skipped
    for stage in ("B", "C", "D"):
        status = report2["stages"].get(stage, "")
        assert "skip" in status.lower() or "skip" in status or "already" in status.lower(), (
            f"阶段 {stage} 未预期跳过: '{status}'"
        )


# ═════════════════════ 异常路径 ═════════════════════


def test_mixdown_empty_sources(tmp_workspace: Path) -> None:
    """混音空音源应抛 ValueError。"""
    with pytest.raises(ValueError, match="至少"):
        mixdown([], tmp_workspace / "mixdown", MixConfig())


def test_invalid_audio(tmp_path: Path) -> None:
    """传入损坏/空文件应报明确错误。"""
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"\x00\x00\x00\x00")  # 无效 WAV 头
    from core.audio_loader import AudioLoader
    loader = AudioLoader()
    with pytest.raises(Exception):
        loader.load(str(bad))


def test_pipeline_stage_failure_does_not_block_later(
    seeded_instruments: Path, tmp_workspace: Path,
) -> None:
    """异常不破坏已产物：阶段B 失败后 C/D 不可执行但 report 记录错误不崩。"""
    # 仅跑阶段A（实际上已种子化，跳过），然后跑阶段C（缺 stems → 抛异常）
    ws = tmp_workspace
    cfg = PipelineConfig(
        workspace=ws,
        songs=[],
        stages=["C"],  # 直接跑 C，缺 stems
        separate_mode="manifest",
        mix_fmt="wav",
    )
    report = run_pipeline(cfg)
    # C 会失败（缺 stems），但 report 仍返回
    assert not report["ok"]
    assert any("C" in e[0] for e in report["errors"])


# ═════════════════════ slow 标记（仅手动/CI 触发） ═════════════════════


@pytest.mark.slow
def test_build_instrument_pool_with_real_demucs(
    tmp_workspace: Path, synthetic_song: Path,
) -> None:
    """⚠ 需要真实 demucs（含首次模型下载）→ 默认跳过。

    手动启用：pytest -m slow（确保网络可用 + audio_separator venv 就绪）。
    """
    from config import AUDIO_SEPARATOR_VENV_PY
    if not Path(AUDIO_SEPARATOR_VENV_PY).is_file():
        pytest.skip("audio_separator venv 不可用")

    fixtures = tmp_workspace / "instruments"
    pool = build_instrument_pool(
        [synthetic_song], fixtures,
        model="htdemucs", shifts=1,
    )
    assert pool.samples_by_key
    for samples in pool.samples_by_key.values():
        for ps in samples:
            assert ps.path.suffix == ".wav"
            # 验证 load_sample 可加载
            from bloodmusic.music.sampler import load_sample
            sam = load_sample(str(ps.path))
            assert len(sam.data) > 0
