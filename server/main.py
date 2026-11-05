import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("BabelDOC-Translator")

ENGINE_SCRIPT = Path(__file__).resolve().parent / "engine.py"


def _find_babeldoc_python() -> str:
    """Locate the Python interpreter that has `babeldoc` installed (preferring uv tool pdf2zh-next)."""
    candidates = []
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        candidates.append(
            Path(appdata) / "uv" / "tools" / "pdf2zh-next" / "Scripts" / "python.exe"
        )
    home = Path.home()
    candidates.extend(
        [
            home / "AppData" / "Roaming" / "uv" / "tools" / "pdf2zh-next" / "Scripts" / "python.exe",
            home / ".local" / "share" / "uv" / "tools" / "pdf2zh-next" / "bin" / "python",
            Path(sys.executable),
        ]
    )
    for cand in candidates:
        if cand.exists():
            return str(cand)
    return sys.executable


BABELDOC_PYTHON = _find_babeldoc_python()
SESSIONS: dict[str, dict] = {}


def _normalize_device(device: str | None, fallback: str = "auto") -> str:
    val = (device or fallback or "auto").strip().lower()
    if val not in ("auto", "gpu", "cpu"):
        return "auto"
    return val


def _resolve_session_dir(session_dir_or_id: str) -> Path:
    p = Path(session_dir_or_id)
    if p.exists() and p.is_dir():
        return p.resolve()
    cand = Path(tempfile.gettempdir()) / f"babeldoc_mcp_{session_dir_or_id}"
    if cand.exists():
        return cand.resolve()
    return p.resolve()


def _load_meta(sdir: Path) -> dict:
    key = str(sdir)
    if key in SESSIONS:
        return SESSIONS[key]
    meta_file = sdir / "session_meta.json"
    if meta_file.exists():
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
            SESSIONS[key] = data
            return data
        except Exception:
            pass
    return {}


def _save_meta(sdir: Path, meta: dict):
    key = str(sdir)
    SESSIONS[key] = meta
    meta_file = sdir / "session_meta.json"
    try:
        tmp = meta_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(meta_file)
    except Exception:
        pass


@mcp.tool()
def extract_segments(
    file: str = "",
    lang_in: str = "en",
    lang_out: str = "zh",
    output_dir: str = "",
    pages: str = "",
    device: str = "auto",
    pdf_path: str = "",
) -> str:
    """
    从 PDF 文件中提取待翻译的文本分段（异步后台执行，内置 IR 缓存快照与 GPU/CPU 加速）。

    Args:
        file: PDF 文件的本地绝对路径。
        lang_in: 源语言代码 (默认: "en")。
        lang_out: 目标语言代码 (默认: "zh")。
        output_dir: 输出目录路径 (默认与原 PDF 同目录)。
        pages: 可选页码范围 (例如 "1-5", "1,3,5-7")。
        device: 加速设备选项。'auto' (默认推荐，自动优先使用 NVIDIA 独显 GPU 并降级 CPU), 'gpu' (强制独显 GPU 加速), 'cpu' (仅使用 CPU 运算，0 显存占用)。
    """
    target_pdf = file or pdf_path
    if not target_pdf:
        return "Error: `file` path is required."

    pdf_file = Path(target_pdf).resolve()
    if not pdf_file.exists():
        return f"Error: PDF file not found at {target_pdf}"

    out_dir = Path(output_dir).resolve() if output_dir else pdf_file.parent
    device_pref = _normalize_device(device, "auto")

    session_id = str(uuid.uuid4())[:8]
    sdir = (Path(tempfile.gettempdir()) / f"babeldoc_mcp_{session_id}").resolve()
    sdir.mkdir(parents=True, exist_ok=True)

    segments_file = sdir / "segments.json"
    segments_file.write_text("[]", encoding="utf-8")
    log_file = sdir / "extract.log"

    meta = {
        "session_id": session_id,
        "session_dir": str(sdir),
        "pdf_path": str(pdf_file),
        "output_dir": str(out_dir),
        "segments_file": str(segments_file),
        "lang_in": lang_in,
        "lang_out": lang_out,
        "pages": pages,
        "device": device_pref,
        "status": "running",
        "error": None,
        "start_time": time.time(),
        "log_file": str(log_file),
        "translated_cursor": 0,
    }
    _save_meta(sdir, meta)

    def _run_extraction():
        cmd = [
            BABELDOC_PYTHON,
            str(ENGINE_SCRIPT),
            "--mode",
            "extract",
            "--pdf-path",
            str(pdf_file),
            "--session-dir",
            str(sdir),
            "--lang-in",
            lang_in,
            "--lang-out",
            lang_out,
            "--device",
            device_pref,
        ]
        if pages and pages.strip():
            cmd.extend(["--pages", pages.strip()])

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["BABELDOC_DEVICE"] = device_pref

        try:
            with open(log_file, "w", encoding="utf-8") as lf:
                proc = subprocess.Popen(
                    cmd,
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                )
                meta["pid"] = proc.pid
                _save_meta(sdir, meta)
                proc.wait()

            try:
                segments = json.loads(segments_file.read_text(encoding="utf-8"))
            except Exception:
                segments = []

            if proc.returncode != 0 and not segments:
                log_tail = ""
                if log_file.exists():
                    log_tail = log_file.read_text(encoding="utf-8", errors="replace")[-2000:]
                meta["status"] = "failed"
                meta["error"] = f"Process exited with code {proc.returncode}.\nLog tail:\n{log_tail}"
            else:
                meta["status"] = "extracted"
                meta["elapsed"] = round(time.time() - meta["start_time"], 2)
            _save_meta(sdir, meta)
        except Exception as e:
            meta["status"] = "failed"
            meta["error"] = str(e)
            _save_meta(sdir, meta)

    thread = threading.Thread(target=_run_extraction, daemon=True)
    thread.start()

    return json.dumps(
        {
            "status": "extracting",
            "session_dir": str(sdir),
            "session_id": session_id,
            "device": device_pref,
            "engine": "fast-in-memory-v2",
            "message": (
                f"Extraction started in background (device={device_pref}, engine=fast-in-memory-v2). "
                "Call `check_extraction_status(session_dir)` to wait for completion and get segments."
            ),
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def check_extraction_status(
    session_dir: str = "",
    offset: int = 0,
    session_id: str = "",
    wait_seconds: int = 15,
) -> str:
    """
    检查分段提取进度。完成时返回待翻译分段与翻译指令。

    Args:
        session_dir: extract_segments 返回的会话目录路径。
        offset: 分段起始索引 (默认: 0)。
    """
    target_key = session_dir or session_id
    if not target_key:
        return "Error: `session_dir` is required."

    sdir = _resolve_session_dir(target_key)
    meta = _load_meta(sdir)
    if not meta:
        return f"Error: Unknown session '{target_key}'."

    wait_sec = min(max(1, wait_seconds), 25)
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        meta = _load_meta(sdir)
        if meta.get("status") in ("extracted", "completed", "failed"):
            break
        time.sleep(0.5)

    segments_file = sdir / "segments.json"
    raw_segments = []
    if segments_file.exists():
        try:
            raw_segments = json.loads(segments_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    status = meta.get("status", "running")
    if status == "running":
        elapsed = round(time.time() - meta.get("start_time", time.time()), 1)
        log_tail = ""
        log_file = Path(meta.get("log_file", sdir / "extract.log"))
        if log_file.exists():
            lines = log_file.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            if lines:
                log_tail = lines[-1][-200:]
        return json.dumps(
            {
                "status": "extracting",
                "session_dir": str(sdir),
                "device": meta.get("device", "auto"),
                "elapsed_seconds": elapsed,
                "segments_extracted_so_far": len(raw_segments),
                "latest_log": log_tail,
                "message": "Still extracting layout and segments... Call `check_extraction_status` again.",
            },
            ensure_ascii=False,
            indent=2,
        )

    if status == "failed":
        return json.dumps(
            {
                "status": "failed",
                "session_dir": str(sdir),
                "device": meta.get("device", "auto"),
                "error": meta.get("error"),
            },
            ensure_ascii=False,
            indent=2,
        )

    # Return slice starting from offset
    total = len(raw_segments)
    sliced = raw_segments[offset:]
    source_texts = [s["source"] if isinstance(s, dict) else str(s) for s in sliced]

    return json.dumps(
        {
            "status": "extracted",
            "session_dir": str(sdir),
            "device": meta.get("device", "auto"),
            "elapsed_seconds": meta.get("elapsed"),
            "total_segments": total,
            "offset": offset,
            "returned_count": len(source_texts),
            "instructions": (
                "请将 segments 中的每条英文文本翻译为目标语言，并调用 `save_translated_segments(session_dir, segments_json)` 保存。\n"
                "重要规则：\n"
                "1. 严禁修改或删除 `{v1}`, `{v2}` 或 `<style id='1'>...</style>` 等占位符！\n"
                "2. 译文数量必须与原分段 1:1 严格对齐（支持分批多次调用 save_translated_segments）。\n"
                "3. 全部保存后调用 `assemble_translated(session_dir)` 生成最终 PDF。"
            ),
            "segments": source_texts,
            "segment_objects": sliced,
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def save_translated_segments(
    session_dir: str = "",
    segments_json: str = "",
    session_id: str = "",
    translations_json: str = "",
) -> str:
    """
    保存翻译后的文本分段到当前会话。支持分批多次调用累加保存。

    Args:
        session_dir: extract_segments 返回的会话目录路径。
        segments_json: 翻译后的分段 JSON 字符串（支持字符串数组 `["译文1", "译文2"]`、对象数组 `[{"id": 0, "translated": "..."}]` 或字典 `{"0": "..."}`）。
    """
    target_key = session_dir or session_id
    payload_str = segments_json or translations_json
    if not target_key:
        return "Error: `session_dir` is required."

    sdir = _resolve_session_dir(target_key)
    meta = _load_meta(sdir)
    segments_file = sdir / "segments.json"
    if not segments_file.exists():
        return f"Error: segments.json not found in {sdir}"

    segments = json.loads(segments_file.read_text(encoding="utf-8"))
    try:
        data = json.loads(payload_str)
    except Exception as e:
        return f"Error parsing segments_json: {e}"

    updated = 0
    if isinstance(data, list):
        if all(isinstance(x, str) for x in data):
            # If len matches total segments, overwrite from 0; otherwise append from first untranslated cursor
            if len(data) == len(segments):
                start_idx = 0
            else:
                # Find first untranslated segment index
                first_untranslated = next(
                    (i for i, s in enumerate(segments) if not s.get("translated")),
                    meta.get("translated_cursor", 0),
                )
                start_idx = first_untranslated
            for offset_i, text_val in enumerate(data):
                target_idx = start_idx + offset_i
                if 0 <= target_idx < len(segments):
                    segments[target_idx]["translated"] = text_val
                    updated += 1
            meta["translated_cursor"] = start_idx + updated
        else:
            id_map = {}
            for idx, item in enumerate(data):
                if isinstance(item, dict) and "id" in item and "translated" in item:
                    id_map[int(item["id"])] = str(item["translated"])
                elif isinstance(item, str):
                    id_map[idx] = item
            for seg in segments:
                sid = int(seg["id"])
                if sid in id_map:
                    seg["translated"] = id_map[sid]
                    updated += 1
    elif isinstance(data, dict):
        id_map = {int(k): str(v) for k, v in data.items()}
        for seg in segments:
            sid = int(seg["id"])
            if sid in id_map:
                seg["translated"] = id_map[sid]
                updated += 1
    else:
        return "Error: segments_json must be a JSON list or dict."

    segments_file.write_text(
        json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _save_meta(sdir, meta)

    translated_total = sum(1 for s in segments if s.get("translated"))
    remaining = len(segments) - translated_total
    return json.dumps(
        {
            "status": "saved",
            "session_dir": str(sdir),
            "updated_in_this_call": updated,
            "translated_count": translated_total,
            "total_segments": len(segments),
            "remaining_untranslated": remaining,
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def assemble_translated(
    session_dir: str = "",
    glossary_json: str = "",
    device: str = "",
    session_id: str = "",
    output_dir: str = "",
    no_dual: bool = False,
    no_mono: bool = False,
) -> str:
    """
    使用已保存的翻译分段组装并生成最终的 PDF（自动复用 Pass 1 IR 缓存快照，秒级回填排版）。

    Args:
        session_dir: 会话目录路径。
        glossary_json: 可选术语表 JSON 字符串。
        device: (可选) 加速设备选项：'auto', 'gpu', 'cpu'。若留空则沿用提取时设定的设备。
    """
    target_key = session_dir or session_id
    if not target_key:
        return "Error: `session_dir` is required."

    sdir = _resolve_session_dir(target_key)
    meta = _load_meta(sdir)
    if not meta:
        return f"Error: Unknown session '{target_key}'."

    pdf_file = Path(meta["pdf_path"])
    out_path = Path(output_dir).resolve() if output_dir else Path(meta.get("output_dir", pdf_file.parent)).resolve()
    out_path.mkdir(parents=True, exist_ok=True)

    device_pref = _normalize_device(device, meta.get("device", "auto"))
    assemble_log = sdir / "assemble.log"

    meta["assemble_status"] = "running"
    meta["assemble_error"] = None
    meta["assemble_start_time"] = time.time()
    meta["assemble_log"] = str(assemble_log)
    meta["output_dir"] = str(out_path)
    meta["assemble_device"] = device_pref
    _save_meta(sdir, meta)

    def _run_assembly():
        cmd = [
            BABELDOC_PYTHON,
            str(ENGINE_SCRIPT),
            "--mode",
            "assemble",
            "--pdf-path",
            str(pdf_file),
            "--session-dir",
            str(sdir),
            "--output-dir",
            str(out_path),
            "--lang-in",
            meta.get("lang_in", "en"),
            "--lang-out",
            meta.get("lang_out", "zh"),
            "--device",
            device_pref,
        ]
        pages = meta.get("pages", "")
        if pages and pages.strip():
            cmd.extend(["--pages", pages.strip()])
        if no_dual:
            cmd.append("--no-dual")
        if no_mono:
            cmd.append("--no-mono")

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["BABELDOC_DEVICE"] = device_pref

        try:
            with open(assemble_log, "w", encoding="utf-8") as lf:
                proc = subprocess.Popen(
                    cmd,
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                )
                meta["assemble_pid"] = proc.pid
                _save_meta(sdir, meta)
                proc.wait()

            stem = pdf_file.stem
            generated = list(out_path.glob(f"{stem}*mono*.pdf")) + list(
                out_path.glob(f"{stem}*dual*.pdf")
            )

            if proc.returncode != 0 and not generated:
                log_tail = ""
                if assemble_log.exists():
                    log_tail = assemble_log.read_text(encoding="utf-8", errors="replace")[-2000:]
                meta["assemble_status"] = "failed"
                meta["assemble_error"] = (
                    f"Process exited with code {proc.returncode}.\nLog tail:\n{log_tail}"
                )
            else:
                meta["assemble_status"] = "completed"
                meta["assemble_elapsed"] = round(
                    time.time() - meta["assemble_start_time"], 2
                )
                meta["generated_files"] = [str(p) for p in generated]
            _save_meta(sdir, meta)
        except Exception as e:
            meta["assemble_status"] = "failed"
            meta["assemble_error"] = str(e)
            _save_meta(sdir, meta)

    thread = threading.Thread(target=_run_assembly, daemon=True)
    thread.start()

    return json.dumps(
        {
            "status": "assembling",
            "session_dir": str(sdir),
            "device": device_pref,
            "engine": "fast-in-memory-v2",
            "message": (
                f"PDF assembly started in background (device={device_pref}, engine=fast-in-memory-v2). "
                "Call `check_assembly_status(session_dir)` to wait for completion."
            ),
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def check_assembly_status(
    session_dir: str = "",
    session_id: str = "",
    wait_seconds: int = 15,
) -> str:
    """
    检查 PDF 组装进度。

    Args:
        session_dir: extract_segments 返回的会话目录路径。
    """
    target_key = session_dir or session_id
    if not target_key:
        return "Error: `session_dir` is required."

    sdir = _resolve_session_dir(target_key)
    meta = _load_meta(sdir)
    if not meta or "assemble_status" not in meta:
        return f"Error: `assemble_translated` has not been called for session '{target_key}'."

    wait_sec = min(max(1, wait_seconds), 25)
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        meta = _load_meta(sdir)
        if meta.get("assemble_status") in ("completed", "failed"):
            break
        time.sleep(0.5)

    status = meta.get("assemble_status", "running")
    if status == "running":
        elapsed = round(time.time() - meta.get("assemble_start_time", time.time()), 1)
        log_tail = ""
        log_file = Path(meta.get("assemble_log", sdir / "assemble.log"))
        if log_file.exists():
            lines = log_file.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            if lines:
                log_tail = lines[-1][-200:]
        return json.dumps(
            {
                "status": "assembling",
                "session_dir": str(sdir),
                "device": meta.get("assemble_device", "auto"),
                "elapsed_seconds": elapsed,
                "latest_log": log_tail,
                "message": "Still typesetting and rendering PDF... Call `check_assembly_status` again.",
            },
            ensure_ascii=False,
            indent=2,
        )

    if status == "failed":
        return json.dumps(
            {
                "status": "failed",
                "session_dir": str(sdir),
                "device": meta.get("assemble_device", "auto"),
                "error": meta.get("assemble_error"),
            },
            ensure_ascii=False,
            indent=2,
        )

    return json.dumps(
        {
            "status": "completed",
            "session_dir": str(sdir),
            "device": meta.get("assemble_device", "auto"),
            "elapsed_seconds": meta.get("assemble_elapsed"),
            "output_dir": meta.get("output_dir"),
            "generated_files": meta.get("generated_files", []),
        },
        ensure_ascii=False,
        indent=2,
    )


def main():
    mcp.run()


if __name__ == "__main__":
    main()
