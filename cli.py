# -*- coding: utf-8 -*-
"""
cli.py —— 纯音乐制作流水线 CLI 入口（T6）

子命令：
  doctor   环境诊断（依赖隔离 / remix-bloodmusic 可用性 / audio_separator venv）
  run      运行完整四阶段流水线 A→B→C→D
  stage-a  仅阶段A：拆歌 → 乐器池
  stage-b  仅阶段B：单音源导出
  stage-c  仅阶段C：混音 + 分轨保留
  stage-d  仅阶段D：后期重分离（模式A manifest / 模式B algo）

全局开关：--separate-mode {manifest|algo}（决策⑦ 显式开关，默认 manifest）。
设计依据：docs/design.md §2.6（PipelineConfig / run_pipeline）、§5（CLI 交互要点）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Windows 控制台编码修复：doctor 等输出含 ✓/✗ 符号，GBK 代码页无法编码
if sys.stdout and getattr(sys.stdout, "encoding", "").lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 扁平米模块导入：把 cli.py 自身所在目录插入 sys.path 首位
_PROJ_ROOT = Path(__file__).resolve().parent
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

from config import (
    bootstrap_env,
    HONESTY_DISCLAIMER,
    AUDIO_SEPARATOR_VENV_PY,
    REMIX_BIN,
)

# 重型依赖（orchestrator → instrument_pool → bloodmusic）惰性导入：
# doctor / tasks 子命令无需重型包即可运行（P3 任务日志审计零重依赖）。
bootstrap_env()


# ──────────────────────────── 辅助 ────────────────────────────


def _parse_track_specs(specs_file: str | None, demo: bool) -> list:
    """从 JSON 文件或 --demo 生成 TrackSpec 列表。"""
    from source_maker import TrackSpec
    if demo:
        return [
            TrackSpec(name="bass_line", kind="synth", bpm=120, duration_sec=4.0),
            TrackSpec(name="pad_chord", kind="synth", bpm=120, duration_sec=4.0),
            TrackSpec(name="lead_arp", kind="synth", bpm=120, duration_sec=4.0),
        ]
    if specs_file:
        with open(specs_file, encoding="utf-8") as f:
            raw = json.load(f)
        specs: list[TrackSpec] = []
        for item in raw:
            if "pattern_source" in item and item["pattern_source"]:
                item["pattern_source"] = Path(item["pattern_source"])
            specs.append(TrackSpec(**item))
        return specs
    return []


def _parse_stages(raw: str) -> list[str]:
    """解析 --stages 参数（如 'A,B,C' → ['A','B','C']）。"""
    parts = [s.strip().upper() for s in raw.split(",") if s.strip()]
    valid = {"A", "B", "C", "D"}
    result = [p for p in parts if p in valid]
    return result if result else ["A", "B", "C", "D"]


# ──────────────────────────── 命令实现 ────────────────────────────


def cmd_doctor(_args: argparse.Namespace) -> None:
    """环境诊断：打印各依赖状态。"""
    print("═══ audio_pipeline 环境诊断 ═══")
    print(f"解释器: {sys.executable}")
    print()

    # R1: torch 不在编排进程
    torch_loaded = "torch" in sys.modules
    print(f"[{'✓' if not torch_loaded else '✗'}] torch 在编排进程: {torch_loaded}")
    if torch_loaded:
        print("  ⚠ 违反 R1 依赖隔离！torch 不应在编排层出现。")
    print()

    # remix / bloodmusic
    try:
        from core.audio_mixer import AudioMixer  # noqa: F401
        from core.audio_loader import AudioLoader  # noqa: F401
        print("[✓] remix.core (AudioMixer / AudioLoader) 可导入")
    except Exception as exc:
        print(f"[✗] remix 导入失败: {exc}")
    try:
        import bloodmusic.bridge  # noqa: F401
        from bloodmusic.music.sampler import load_sample  # noqa: F401
        print("[✓] bloodmusic.bridge / music.sampler 可导入")
    except Exception as exc:
        print(f"[✗] bloodmusic 导入失败: {exc}")
    print()

    # pydub / ffmpeg pin
    try:
        from pydub import AudioSegment
        conv = AudioSegment.converter
        probe = AudioSegment.ffprobe
        print(f"[✓] pydub converter: {conv}")
        print(f"[✓] pydub ffprobe  : {probe}")
        ffmpeg_ok = conv and Path(conv).is_file()
        print(f"[{'✓' if ffmpeg_ok else '✗'}] ffmpeg.exe 存在: {ffmpeg_ok}")
    except Exception as exc:
        print(f"[✗] pydub 初始化失败: {exc}")
    print()

    # audio_separator venv
    sep_py = Path(AUDIO_SEPARATOR_VENV_PY)
    if sep_py.is_file():
        print(f"[✓] audio_separator venv: {AUDIO_SEPARATOR_VENV_PY}")
    else:
        print(f"[✗] audio_separator venv 缺失: {AUDIO_SEPARATOR_VENV_PY}")
    print()

    # 诚实性声明
    print("─── 诚实性声明 ───")
    print(HONESTY_DISCLAIMER)
    print()


def cmd_run(args: argparse.Namespace) -> None:
    """运行完整四阶段流水线。"""
    from orchestrator import PipelineConfig, run_pipeline
    stages = _parse_stages(args.stages)
    cfg = PipelineConfig(
        workspace=Path(args.workspace),
        songs=[Path(s) for s in (args.songs or [])],
        midi_root=Path(args.midi_root),
        sep_model=args.model,
        sep_shifts=args.shifts,
        separate_mode=args.separate_mode,
        stages=stages,
        track_specs=_parse_track_specs(args.track_specs, args.demo),
        mix_fmt=args.mix_fmt,
    )
    print(f"启动流水线（{', '.join(stages)}），分离模式: {cfg.separate_mode}")
    print(f"工作区: {cfg.workspace}")
    report = run_pipeline(cfg)
    print()
    print("═══ 流水线报告 ═══")
    for stage, status in report["stages"].items():
        print(f"  阶段 {stage}: {status}")
    if report["errors"]:
        print(f"  错误 ({len(report['errors'])}):")
        for s, msg in report["errors"]:
            print(f"    [{s}] {msg[:120]}")
    print(f"  整体 {'✓ OK' if report['ok'] else '✗ 有错误'}")
    if not report["ok"]:
        sys.exit(1)


def cmd_stage_a(args: argparse.Namespace) -> None:
    """仅阶段A。"""
    from instrument_pool import build_instrument_pool
    fixtures = Path(args.workspace) / "instruments"
    fixtures.mkdir(parents=True, exist_ok=True)
    pool = build_instrument_pool(
        [Path(s) for s in args.songs], fixtures,
        model=args.model, shifts=args.shifts,
    )
    n = sum(len(v) for v in pool.samples_by_key.values())
    print(f"阶段A 完成：{n} 个样本（{len(pool.samples_by_key)} 个乐器族）")


def cmd_stage_b(args: argparse.Namespace) -> None:
    """仅阶段B。"""
    from instrument_pool import InstrumentPool
    ws = Path(args.workspace)
    fixtures = ws / "instruments"
    stems_dir = ws / "stems"
    stems_dir.mkdir(parents=True, exist_ok=True)
    pool = InstrumentPool.load(fixtures) if fixtures.is_dir() else InstrumentPool(fixtures)
    specs = _parse_track_specs(args.track_specs, args.demo)
    if not specs:
        print("[阶段B] 无 TrackSpec（请使用 --demo 或 --track-specs）。", file=sys.stderr)
        sys.exit(1)
    for spec in specs:
        out = make_source(spec, pool, stems_dir)
        print(f"  → {out}")
    print(f"阶段B 完成：{len(specs)} 条音源。")


def cmd_stage_c(args: argparse.Namespace) -> None:
    """仅阶段C。"""
    ws = Path(args.workspace)
    stems_dir = ws / "stems"
    mixdown_dir = ws / "mixdown"
    sources = sorted(
        [p for ext in ("*.wav", "*.ogg", "*.flac") for p in stems_dir.glob(ext)]
    )
    if not sources:
        print("[阶段C] 无可用音源（请先运行阶段B）。", file=sys.stderr)
        sys.exit(1)
    from mixdown import mixdown
    from core.audio_mixer import MixConfig
    result = mixdown(
        sources, mixdown_dir, MixConfig(),
        fmt=args.mix_fmt,
        separate_mode=args.separate_mode,
    )
    print(f"阶段C 完成 → {result.song_path}")
    print(f"  _sources: {result.sources_dir}")
    print(f"  manifest: {result.manifest_path}")


def cmd_stage_d(args: argparse.Namespace) -> None:
    """仅阶段D。"""
    ws = Path(args.workspace)
    mixdown_dir = ws / "mixdown"
    separated_dir = ws / "separated"
    manifest_path = mixdown_dir / "manifest.json"
    if not manifest_path.is_file():
        print("[阶段D] manifest.json 不存在（请先运行阶段C）。", file=sys.stderr)
        sys.exit(1)
    from manifest import read_manifest
    mani = read_manifest(manifest_path)
    song = mixdown_dir / mani.song
    if not song.is_file():
        print(f"[阶段D] 成品歌缺失: {song}", file=sys.stderr)
        sys.exit(1)
    from separate_back import separate_back
    res = separate_back(
        song, separated_dir,
        mode=args.separate_mode,
        manifest=manifest_path,
        model=args.model,
        shifts=args.shifts,
    )
    print(f"阶段D 完成（{res['fidelity']}）→ {len(res['files'])} 条分轨。")


def cmd_chain(args: argparse.Namespace) -> None:
    """S6：提示词→混音→电台 一键链。"""
    from pathlib import Path
    from musickit_chain import ChainConfig, run_chain
    from pipelog import PipelineLog

    ws = Path(args.workspace)
    ws.mkdir(parents=True, exist_ok=True)
    cfg = ChainConfig(
        workspace=ws, channel=args.channel, style=args.style, bars=args.bars,
        fmt=args.fmt, max_tracks=args.max_tracks, fusion=args.fusion,
        release_safe=args.release_safe, radio_url=args.radio, timeout=args.timeout,
    )
    log = PipelineLog(ws / "pipeline.log")
    report = run_chain(cfg, log)
    for k, v in report["stages"].items():
        print(f"[{k}] {v}")
    for e in report["errors"]:
        print(f"[错误] {e}", file=sys.stderr)
    print(f"\n完成：{'成功' if report['ok'] else '存在失败阶段'}（run_id={report['run_id']}，日志 {ws / 'pipeline.log'}）")


def cmd_tasks(args: argparse.Namespace) -> None:
    """任务日志审计：读取 <workspace>/pipeline.log（P3 日志即事实源）。"""
    from pipelog import PipelineLog

    ws = Path(args.workspace)
    log_path = ws / "pipeline.log"
    if not log_path.is_file():
        print(f"[tasks] 无任务日志: {log_path}", file=sys.stderr)
        sys.exit(1)
    log = PipelineLog(log_path)
    entries = log.read(limit=args.limit)
    if args.json:
        print(json.dumps(entries, ensure_ascii=False, indent=2))
        return
    print(f"任务日志 {log_path}（最近 {len(entries)} 条）：")
    for e in entries:
        stage = e.get("stage", "-")
        extra = " ".join(
            f"{k}={v}" for k, v in e.items()
            if k not in ("seq", "ts", "type", "stage", "run_id")
        )
        print(f"  #{e['seq']} {e['ts']} [{e['type']}] 阶段{stage} {extra}")
    runs = sorted({e.get("run_id") for e in entries if e.get("run_id")})
    errors = [e for e in entries if e["type"] == "stage_error"]
    print(f"  涉及运行 {len(runs)} 次，阶段错误 {len(errors)} 条")


# ──────────────────────────── 主入口 ────────────────────────────


def main(argv: list[str] | None = None) -> None:
    """CLI 主入口（可传 argv 用于测试注入）。"""
    parser = argparse.ArgumentParser(
        prog="audio_pipeline",
        description="纯音乐制作流水线编排工具 —— 串联拆歌·合成·混音·重分离四阶段",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # doctor
    sub.add_parser("doctor", help="环境诊断（依赖隔离 / 外部工具可用性）")

    # run
    p_run = sub.add_parser("run", help="运行完整流水线 A→B→C→D")
    p_run.add_argument("--workspace", required=True, help="工作区根目录")
    p_run.add_argument("--songs", nargs="+", default=[], help="阶段A 输入歌曲")
    p_run.add_argument("--midi-root", default="G:/midi", help="MIDI 素材根（默认 G:/midi）")
    p_run.add_argument("--model", default="htdemucs", help="分离模型（默认 htdemucs）")
    p_run.add_argument("--shifts", type=int, default=2, help="demucs shifts（默认 2）")
    p_run.add_argument(
        "--separate-mode", choices=("manifest", "algo"), default="manifest",
        help="阶段D 分离模式：manifest（真无损，默认）/ algo（有损算法）",
    )
    p_run.add_argument(
        "--stages", default="A,B,C,D",
        help="要运行的阶段（逗号分隔，如 A,B,C,D；默认全部）",
    )
    p_run.add_argument("--track-specs", default=None, help="阶段B TrackSpec JSON 文件")
    p_run.add_argument("--mix-fmt", default="wav", choices=("wav", "ogg", "flac"), help="混音格式")
    p_run.add_argument(
        "--demo", action="store_true",
        help="使用内置演示 TrackSpec（3 条 synth 分轨，4 秒）",
    )

    # stage-a
    p_a = sub.add_parser("stage-a", help="仅阶段A：拆真实歌 → 乐器池")
    p_a.add_argument("--workspace", required=True)
    p_a.add_argument("--songs", nargs="+", required=True)
    p_a.add_argument("--model", default="htdemucs")
    p_a.add_argument("--shifts", type=int, default=2)

    # stage-b
    p_b = sub.add_parser("stage-b", help="仅阶段B：单音源导出")
    p_b.add_argument("--workspace", required=True)
    p_b.add_argument("--track-specs", default=None)
    p_b.add_argument("--demo", action="store_true")

    # stage-c
    p_c = sub.add_parser("stage-c", help="仅阶段C：混音 + 分轨保留")
    p_c.add_argument("--workspace", required=True)
    p_c.add_argument("--mix-fmt", default="wav", choices=("wav", "ogg", "flac"))
    p_c.add_argument(
        "--separate-mode", choices=("manifest", "algo"), default="manifest",
        help="写入 manifest 的分离模式标识（默认 manifest）",
    )

    # stage-d
    p_d = sub.add_parser("stage-d", help="仅阶段D：后期重分离")
    p_d.add_argument("--workspace", required=True)
    p_d.add_argument(
        "--separate-mode", choices=("manifest", "algo"), default="manifest",
        help="manifest（真无损）/ algo（算法分离，有损）",
    )
    p_d.add_argument("--model", default="htdemucs")
    p_d.add_argument("--shifts", type=int, default=2)

    # chain（S6：提示词→混音→电台 一键链）
    p_ch = sub.add_parser("chain", help="musickit 一键链：提示词校验→渲染混音→电台开播")
    p_ch.add_argument("--workspace", required=True, help="工作区（渲染产物目录）")
    p_ch.add_argument("--channel", default="random", help="混音师 slug（24 位之一 / random）")
    p_ch.add_argument("--style", default=None, help="提示词风格 key（63 流派之一，可选）")
    p_ch.add_argument("--bars", type=int, default=32)
    p_ch.add_argument("--fmt", default="mp3", choices=("mp3", "wav"))
    p_ch.add_argument("--max-tracks", type=int, default=5)
    p_ch.add_argument("--fusion", action="store_true")
    p_ch.add_argument("--release-safe", action="store_true")
    p_ch.add_argument("--radio", default="http://localhost:8000", help="Node 电台地址")
    p_ch.add_argument("--timeout", type=int, default=900)

    # tasks（P3 任务日志审计）
    p_t = sub.add_parser("tasks", help="审计 <workspace>/pipeline.log 任务事件日志")
    p_t.add_argument("--workspace", required=True, help="工作区根目录")
    p_t.add_argument("--limit", type=int, default=100, help="显示最近条数")
    p_t.add_argument("--json", action="store_true", help="JSON 输出")

    args = parser.parse_args(argv)

    if args.command == "doctor":
        cmd_doctor(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "stage-a":
        cmd_stage_a(args)
    elif args.command == "stage-b":
        cmd_stage_b(args)
    elif args.command == "stage-c":
        cmd_stage_c(args)
    elif args.command == "stage-d":
        cmd_stage_d(args)
    elif args.command == "tasks":
        cmd_tasks(args)
    elif args.command == "chain":
        cmd_chain(args)


if __name__ == "__main__":
    main()
