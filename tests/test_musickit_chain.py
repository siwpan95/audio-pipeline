# -*- coding: utf-8 -*-
"""test_musickit_chain.py —— 一键链测试（S6）：假渲染器 + 假电台 + 全链日志审计。"""
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from musickit_chain import ChainConfig, run_chain, stage_prompt
from pipelog import PipelineLog


def _fake_renderer(root: Path, ok: bool = True):
    script = root / "render_one.py"
    out = root / "out"
    out.mkdir(exist_ok=True)
    if ok:
        script.write_text(
            'import json, os, sys\n'
            'out = sys.argv[sys.argv.index("--out") + 1]\n'
            'os.makedirs(out, exist_ok=True)\n'
            'p = os.path.join(out, "track.mp3")\n'
            'open(p, "wb").write(b"ID3")\n'
            'print(json.dumps({"ok": True, "file": p, "style": "Jaycen Joshua", "label": "Darksynth", "subgenre": "S", "bpm": 110, "duration_s": 17.5}))\n', encoding="utf-8")
    else:
        script.write_text('import sys\nsys.exit(3)\n', encoding="utf-8")
    return script


class _FakeRadio:
    """极简假电台：记录 /api/musickit 收到的 action。"""
    def __init__(self):
        self.requests = []
        self._server = None

    def start(self):
        holder = self
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/api/status":
                    body = b'{"ok":true}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                data = self.rfile.read(n)
                try:
                    holder.requests.append(json.loads(data))
                except Exception:
                    holder.requests.append({"raw": data.decode("utf-8", "replace")})
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *a):
                pass
        self._server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self):
        if self._server:
            self._server.shutdown()


def test_full_chain_with_fakes():
    tmp = Path(tempfile.mkdtemp())
    renderer = _fake_renderer(tmp)
    radio = _FakeRadio()
    radio_url = radio.start()
    try:
        ws = tmp / "ws"
        ws.mkdir()
        log = PipelineLog(ws / "pipeline.log")
        cfg = ChainConfig(workspace=ws, channel="joshua", style=None, bars=8,
                          renderer=renderer, style_cli=tmp / "style_plugin.py",
                          radio_url=radio_url)
        report = run_chain(cfg, log)
        assert report["ok"] is True, report["errors"]
        assert "R" in report["stages"] and "track.mp3" in report["stages"]["R"]
        assert "电台已开播" in report["stages"]["B"]
        assert radio.requests and radio.requests[0]["action"] == "play"
        # 全链日志：run_start + P/R/B stage_done + run_end
        events = [json.loads(l) for l in (ws / "pipeline.log").read_text(encoding="utf-8").strip().split("\n")]
        types = [e["type"] for e in events]
        assert types[0] == "run_start" and types[-1] == "run_end"
        assert types.count("stage_done") == 3
        assert {e["stage"] for e in events if e["type"] == "stage_done"} == {"P", "R", "B"}
        assert all(e["run_id"] == report["run_id"] for e in events)
    finally:
        radio.stop()


def test_render_fail_marks_error():
    tmp = Path(tempfile.mkdtemp())
    renderer = _fake_renderer(tmp, ok=False)
    ws = tmp / "ws"
    ws.mkdir()
    cfg = ChainConfig(workspace=ws, channel="random", style=None, bars=8,
                      renderer=renderer, style_cli=tmp / "style_plugin.py",
                      radio_url="http://127.0.0.1:1")
    report = run_chain(cfg)
    assert report["ok"] is False
    assert any("R:" in e for e in report["errors"])
    events = [json.loads(l) for l in (ws / "pipeline.log").read_text(encoding="utf-8").strip().split("\n")]
    assert any(e["type"] == "stage_error" and e["stage"] == "R" for e in events)


def test_unknown_style_fail_loud():
    """P 段：未知风格 key 当场失败。"""
    tmp = Path(tempfile.mkdtemp())
    style_cli = tmp / "style_plugin.py"
    style_cli.write_text(
        'import json, sys\n'
        'print(json.dumps({"styles": [{"key": "phonk"}, {"key": "lofi"}]}))\n', encoding="utf-8")
    cfg = ChainConfig(workspace=tmp / "ws", style="not_a_style", style_cli=style_cli)
    ok, detail = stage_prompt(cfg)
    assert ok is False
    assert "未知风格" in detail


def test_radio_down_not_fatal():
    """B 段：电台未运行 → 不阻断，仅完成渲染。"""
    tmp = Path(tempfile.mkdtemp())
    renderer = _fake_renderer(tmp)
    ws = tmp / "ws"
    ws.mkdir()
    cfg = ChainConfig(workspace=ws, channel="random", style=None, bars=8,
                      renderer=renderer, style_cli=tmp / "style_plugin.py",
                      radio_url="http://127.0.0.1:1")
    report = run_chain(cfg)
    assert report["ok"] is True
    assert "仅完成渲染" in report["stages"]["B"]
