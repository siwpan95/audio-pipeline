# -*- coding: utf-8 -*-
"""
config.py —— 编排工具配置与环境解析（T1）

职责（仅依赖 os / sys / pathlib / subprocess 标准库，零重包）：
  - 外部项目根路径与 venv 解释器路径常量
  - 默认分离模型 / shifts / 输出格式
  - bootstrap_env()：在 import pydub / remix / bloodmusic 之前调用一次，
    负责 PATH 注入 + pydub pin + sys.path 注入（依赖隔离落地）。
  - HONESTY_DISCLAIMER：诚实性文案，CLI / 日志 / manifest 三处统一引用。
  - run_separator()：封装 audio_separator 子进程调用（torch 永不进本进程）。

设计依据：docs/design.md §2.1、§4（依赖隔离）。
"""

from __future__ import annotations

import os
import sys
import subprocess
from pathlib import Path

# ───────────────────────── 外部项目路径常量 ─────────────────────────

# 2026-09-23 去本地化：同级仓根由本文件位置推导（…/repos/<本仓> → …/repos），
# 均可用同名环境变量覆盖；原路径已失效。
REPOS_ROOT = Path(__file__).resolve().parent.parent

# remix 根：仅做 sys.path 注入后 import core.audio_mixer / core.audio_loader
REMIX_ROOT = os.environ.get("REMIX_ROOT") or str(REPOS_ROOT / "remix")
# bloodmusic 根（= 仓 audio-generation；editable 已装，这里仅作文档/兜底）
BLOODMUSIC_ROOT = os.environ.get("BLOODMUSIC_ROOT") or str(REPOS_ROOT / "audio-generation")
# audio_separator 根：仅通过 subprocess 调用其自有 venv，绝不 import
AUDIO_SEPARATOR_ROOT = os.environ.get("AUDIO_SEPARATOR_ROOT") or str(REPOS_ROOT / "audio-separator")
# audio_separator 的 venv 解释器（torch 重包留在自有 venv）
AUDIO_SEPARATOR_VENV_PY = os.path.join(
    AUDIO_SEPARATOR_ROOT, "venv", "Scripts", "python.exe"
)
# 若 venv 不存在则回退到当前解释器（仅用于诊断，真实运行要求 venv 就绪）
if not os.path.isfile(AUDIO_SEPARATOR_VENV_PY):
    AUDIO_SEPARATOR_VENV_PY = sys.executable

# remix 自带的 ffmpeg / ffprobe（仅用这一份，不污染系统 PATH）
REMIX_BIN = os.path.join(REMIX_ROOT, "bin")

# ───────────────────────── MIDI 素材根（阶段B 只读输入）─────────────────────────

def default_midi_root() -> Path:
    """MIDI 素材根：环境变量 MIDI_ROOT 优先，其次同级仓 repos/midi。

    2026-09-23 去本地化：原默认值指向已拆分的顶层分类目录，现按同级仓推导。
    """
    env = os.environ.get("MIDI_ROOT")
    return Path(env) if env else (REPOS_ROOT / "midi")


# ───────────────────────── 默认分离参数 ─────────────────────────

DEFAULT_SEP_MODEL = "htdemucs"      # P1-1：可选 "htdemucs_6s"（吉他/钢琴更丰富，质量较弱）
DEFAULT_SEP_SHIFTS = 2
DEFAULT_OUTPUT_FMT = "wav"          # 阶段A 强制 wav；阶段B/C 可 wav/ogg/flac

# ───────────────────────── 诚实性声明（三处统一引用） ─────────────────────────

HONESTY_DISCLAIMER = (
    "⚠ 混音不可逆；模式A=复用源分轨(真无损100%)，"
    "模式B=算法重分离(有损近似, SDR≈9–12dB，非无损还原)"
)

# 模式A 还原说明（manifest note 细分）
HONESTY_MODE_A = (
    "模式A：复用混音时保留的原始分轨（逐字节一致），100% 保真，非算法重分离。"
)
# 模式B 有损说明
HONESTY_MODE_B = (
    "模式B：对成品歌算法重分离（有损近似, SDR≈9–12dB），非无损还原。"
)

# 受支持的音频格式（与 bloodmusic / remix 对齐）
SUPPORTED_AUDIO_FORMATS = ("wav", "ogg", "flac")

# demucs 输出目录下的模型子目录名（demucs 默认把结果放到 <out>/<model>/<track>/）
# 这里仅用于「subprocess 产物 → 乐器池目录」的搬运时定位。

_BOOTSTRAPPED = False


def bootstrap_env() -> None:
    """在 import pydub / remix / bloodmusic 之前调用一次（幂等）。

    1) 把 REMIX_BIN 注入 os.environ["PATH"]；
    2) pin pydub.AudioSegment.converter / ffprobe 到 REMIX_BIN 的 ffmpeg / ffprobe；
    3) sys.path.insert(0, REMIX_ROOT)，使 `from core.audio_mixer import AudioMixer` 可用；
    4) import bloodmusic / bloodmusic.bridge / bloodmusic.music.sampler（验证可用）。

    ※ audio_separator 永不 import（torch 留在自有 venv，仅 subprocess 调用）。
    """
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return

    # 1) PATH 注入（remix 自带 ffmpeg）
    old_path = os.environ.get("PATH", "")
    if REMIX_BIN not in old_path.split(os.pathsep):
        os.environ["PATH"] = REMIX_BIN + os.pathsep + old_path

    # 2) pydub pin（仅 PATH 不够稳，pin 路径最可靠）
    try:
        from pydub import AudioSegment  # 仅依赖 audioop（已装 audioop-lts），无 torch
        AudioSegment.converter = os.path.join(REMIX_BIN, "ffmpeg.exe")
        AudioSegment.ffprobe = os.path.join(REMIX_BIN, "ffprobe.exe")
    except Exception as exc:  # pragma: no cover - 环境异常时给出明确提示
        sys.stderr.write(
            f"[audio_pipeline] 警告：pin pydub 失败（{exc}）。"
            "请确认 audioop-lts 已安装且 remix/bin 含 ffmpeg。\n"
        )

    # 3) remix 根注入 sys.path（先置顶一次）
    def _pin_remix_path() -> None:
        """把 REMIX_ROOT 稳定置于 sys.path 首位。

        2026-09-23 实测：bloodmusic（命名空间包）在 **import 时**会把
        site-packages\\bloodmusic 插到 sys.path[0]，而它与 remix 都存在顶层 core/ 包，
        导致 `from core.audio_mixer import AudioMixer` 解析到 bloodmusic 的 core。
        因此必须在 bloodmusic 导入之后再置顶一次。
        """
        if REMIX_ROOT in sys.path:
            sys.path.remove(REMIX_ROOT)
        sys.path.insert(0, REMIX_ROOT)

    _pin_remix_path()

    # 4) 验证 bloodmusic 可用（editable 已装，无需 path 注入）
    try:
        import bloodmusic  # noqa: F401
        import bloodmusic.bridge  # noqa: F401
        from bloodmusic.music import sampler as _sampler  # noqa: F401
    except Exception as exc:  # pragma: no cover
        sys.stderr.write(
            f"[audio_pipeline] 警告：import bloodmusic 失败（{exc}）。"
            "请确认当前解释器已 editable 安装 bloodmusic。\n"
        )

    # 4b) bloodmusic 导入可能改动 sys.path，重新置顶 REMIX_ROOT（见 _pin_remix_path 说明）
    _pin_remix_path()

    _BOOTSTRAPPED = True


def _separator_env() -> dict:
    """构造 audio_separator 子进程环境变量，确保 torch DLL 可被找到。

    参考 audio_separator/src/demucs_runner.py::_subprocess_env：
    把 venv 内的 torch/lib 加入 PATH。
    """
    env = os.environ.copy()
    torch_lib = os.path.join(
        AUDIO_SEPARATOR_ROOT, "venv", "Lib", "site-packages", "torch", "lib"
    )
    paths = env.get("PATH", "")
    if os.path.isdir(torch_lib) and torch_lib not in paths.split(os.pathsep):
        env["PATH"] = torch_lib + os.pathsep + paths
    return env


def run_separator(cmd: list[str], *, timeout: int | None = None) -> tuple[int, str]:
    """运行 audio_separator 子进程（demucs CLI）。

    Args:
        cmd: 完整命令列表，首元素应为 AUDIO_SEPARATOR_VENV_PY。
        timeout: 可选超时（秒）。

    Returns:
        (returncode, combined_output_text)

    Raises:
        不抛出子进程错误，由调用方根据 returncode 处理；
        但 FileNotFoundError（venv 缺失）会原样抛出以暴露环境问题。
    """
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=_separator_env(),
        creationflags=creationflags,
    )
    out_lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        text = line.rstrip("\n")
        if text.strip():
            out_lines.append(text)
    ret = proc.wait(timeout=timeout)
    return ret, "\n".join(out_lines)


def build_separator_command(
    in_path: str | Path,
    out_dir: str | Path,
    model: str,
    shifts: int,
) -> list[str]:
    """组装 demucs 子进程命令（仅全 stem，不加 --two-stems vocals）。

    与 audio_separator/src/demucs_runner.py::build_command 一致：
    [VENV_PY, -u, -m, demucs, --name, model, -o, out_dir, --shifts, N,
     --filename, {track}/{stem}.{ext}, in_path]

    产物默认落在 <out_dir>/<model>/<track_stem>/<stem>.wav。
    """
    return [
        AUDIO_SEPARATOR_VENV_PY,
        "-u",
        "-m",
        "demucs",
        "--name",
        model,
        "-o",
        str(out_dir),
        "--shifts",
        str(int(shifts)),
        "--filename",
        "{track}/{stem}.{ext}",
        str(in_path),
    ]
