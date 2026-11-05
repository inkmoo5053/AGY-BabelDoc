import json
import os
import shutil
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
            try:
                res = subprocess.run(
                    [str(cand), "-c", "import babeldoc"],
                    capture_output=True,
                    timeout=5,
                )
                if res.returncode == 0:
                    return str(cand)
            except Exception:
                pass
    return sys.executable


BABELDOC_PYTHON = _find_babeldoc_python()
SESSIONS: dict[str, dict] = {}


def _normalize_device(device: str | None, fallback: str = "auto") -> str:
    val = (device or fallback or "auto").strip().lower()
    if val not in ("auto", "gpu", "cpu"):
        return "auto"
    return val


@mcp.tool()
def extract_segments(
    pdf_path: str,
    lang_in: str = "en",
    lang_out: str = "zh",
    pages: str = "",
    device: str = "auto",
) -> str:
    """
    Step 1 (Async): Start extracting translatable text segments from a PDF using BabelDOC's layout analysis.
    Because layout analysis (OCR/vision) can take 10-60 seconds, this tool starts the job in the background
    and returns a `session_id` immediately.
     MUST call `check_extraction_status(session_id)` afterwards to wait for completion and get the segments!

    Args:
        pdf_path: Absolute path to the source PDF file.
        lang_in: Source language code (default: "en").
        lang_out: Target language code (default: "zh").
        pages: Optional page range to translate (e.g., "1-3", "1,3,5-7"). Leave empty for all pages.
        device: Inference device for layout analysis: "auto" (default, uses discrete GPU if available else CPU), "gpu" (force discrete GPU), or "cpu" (force CPU with 0 VRAM usage).
    """
    pdf_file = Path(pdf_path).resolve()
    if not pdf_file.exists():
        return f"Error: PDF file not found at {pdf_path}"

    device_pref = _normalize_device(device, "auto")
    session_id = str(uuid.uuid4())[:8]
    session_dir = Path(tempfile.gettempdir()) / f"babeldoc_mcp_{session_id}"
    session_dir.mkdir(parents=True, exist_ok=True)

    segments_file = session_dir / "segments.json"
    segments_file.write_text("[]", encoding="utf-8")
    log_file = session_dir / "extract.log"

    SESSIONS[session_id] = {
        "pdf_path": str(pdf_file),
        "session_dir": str(session_dir),
        "segments_file": str(segments_file),
        "lang_in": lang_in,
        "lang_out": lang_out,
        "pages": pages,
        "device": device_pref,
        "status": "running",
        "error": None,
        "start_time": time.time(),
        "log_file": str(log_file),
    }

    def _run_extraction():
        cmd = [
            BABELDOC_PYTHON,
            str(ENGINE_SCRIPT),
            "--mode",
            "extract",
            "--pdf-path",
            str(pdf_file),
            "--session-dir",
            str(session_dir),
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
                SESSIONS[session_id]["pid"] = proc.pid
                proc.wait()

            try:
                segments = json.loads(segments_file.read_text(encoding="utf-8"))
            except Exception:
                segments = []

            if proc.returncode != 0 and not segments:
                log_tail = ""
                if log_file.exists():
                    log_tail = log_file.read_text(encoding="utf-8", errors="replace")[-2000:]
                SESSIONS[session_id]["status"] = "failed"
                SESSIONS[session_id]["error"] = (
                    f"Process exited with code {proc.returncode}.\nLog tail:\n{log_tail}"
                )
            else:
                SESSIONS[session_id]["status"] = "completed"
                SESSIONS[session_id]["elapsed"] = round(
                    time.time() - SESSIONS[session_id]["start_time"], 2
                )
        except Exception as e:
            SESSIONS[session_id]["status"] = "failed"
            SESSIONS[session_id]["error"] = str(e)

    thread = threading.Thread(target=_run_extraction, daemon=True)
    thread.start()

    return json.dumps(
        {
            "session_id": session_id,
            "status": "running",
            "device": device_pref,
            "engine": "fast-in-memory-v2",
            "message": (
                f"Extraction started in background (device={device_pref}). "
                "Please call `check_extraction_status(session_id)` to poll progress and retrieve segments."
            ),
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def check_extraction_status(session_id: str, wait_seconds: int = 10) -> str:
    """
    Poll the status of a background segment extraction job started by `extract_segments`.
    Wait up to `wait_seconds` (capped at 25s to avoid MCP timeout) for the job to finish.
    If still running, call this tool again until status is 'completed'.
    """
    if session_id not in SESSIONS:
        return f"Error: Unknown session_id '{session_id}'."

    session = SESSIONS[session_id]
    wait_seconds = min(max(1, wait_seconds), 25)

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if session["status"] in ("completed", "failed"):
            break
        time.sleep(0.5)

    segments_file = Path(session["segments_file"])
    segments = []
    if segments_file.exists():
        try:
            segments = json.loads(segments_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    if session["status"] == "running":
        elapsed = int(time.time() - session["start_time"])
        log_tail = ""
        log_file = Path(session["log_file"])
        if log_file.exists():
            lines = (
                log_file.read_text(encoding="utf-8", errors="replace")
                .strip()
                .splitlines()
            )
            if lines:
                log_tail = lines[-1][-200:]
        return json.dumps(
            {
                "session_id": session_id,
                "status": "running",
                "device": session.get("device", "auto"),
                "elapsed_seconds": elapsed,
                "segments_extracted_so_far": len(segments),
                "latest_log": log_tail,
                "message": "Still extracting layout and segments... Please call `check_extraction_status` again.",
            },
            ensure_ascii=False,
            indent=2,
        )

    if session["status"] == "failed":
        return json.dumps(
            {
                "session_id": session_id,
                "status": "failed",
                "device": session.get("device", "auto"),
                "error": session["error"],
            },
            ensure_ascii=False,
            indent=2,
        )

    return json.dumps(
        {
            "session_id": session_id,
            "status": "completed",
            "device": session.get("device", "auto"),
            "elapsed_seconds": session.get("elapsed"),
            "pdf_path": session["pdf_path"],
            "total_segments": len(segments),
            "instructions": (
                "Translate the `source` field of each segment into the target language. "
                "CRITICAL RULES:\n"
                "1. Preserve ALL placeholders like `{v1}`, `{v2}` or `<style id='1'>...</style>` EXACTLY as they appear!\n"
                "2. Do NOT add or remove segments.\n"
                "3. Call `save_translated_segments` (you can do this in batches if there are many segments), "
                "then call `assemble_translated` to generate the final PDF."
            ),
            "segments": segments,
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def save_translated_segments(session_id: str, translations_json: str) -> str:
    """
    Step 2: Save translated segments back into the session.
    Can be called multiple times in batches for large documents.

    Args:
        session_id: The session_id returned by `extract_segments`.
        translations_json: A JSON string representing a list of objects: `[{"id": 0, "translated": "..."}, ...]`,
                           OR a dict mapping id to translation: `{"0": "...", "1": "..."}`.
    """
    if session_id not in SESSIONS:
        return f"Error: Unknown session_id '{session_id}'."

    session = SESSIONS[session_id]
    segments_file = Path(session["segments_file"])
    segments = json.loads(segments_file.read_text(encoding="utf-8"))

    try:
        data = json.loads(translations_json)
    except Exception as e:
        return f"Error parsing translations_json: {e}"

    id_map: dict[int, str] = {}
    if isinstance(data, list):
        for idx, item in enumerate(data):
            if isinstance(item, dict) and "id" in item and "translated" in item:
                id_map[int(item["id"])] = str(item["translated"])
            elif isinstance(item, str):
                id_map[idx] = item
    elif isinstance(data, dict):
        for k, v in data.items():
            id_map[int(k)] = str(v)
    else:
        return "Error: translations_json must be a JSON list or dict."

    updated = 0
    for seg in segments:
        sid = int(seg["id"])
        if sid in id_map:
            seg["translated"] = id_map[sid]
            updated += 1

    segments_file.write_text(
        json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    remaining = sum(1 for s in segments if not s.get("translated"))

    return (
        f"Updated {updated} segments in session '{session_id}'. "
        f"Total segments: {len(segments)}, Remaining untranslated: {remaining}."
    )


@mcp.tool()
def assemble_translated(
    session_id: str,
    output_dir: str = "",
    no_dual: bool = False,
    no_mono: bool = False,
    device: str = "auto",
) -> str:
    """
    Step 3 (Async): Start re-assembling the translated PDF in the background using BabelDOC.
    Returns immediately. You MUST call `check_assembly_status(session_id)` afterwards to wait for the final PDFs!

    Args:
        session_id: The session_id returned by `extract_segments`.
        output_dir: Directory where the translated PDFs (`*.mono.pdf` and `*.dual.pdf`) will be saved.
                    Defaults to the same directory as the original PDF.
        no_dual: If True, do not generate the bilingual side-by-side PDF.
        no_mono: If True, do not generate the monolingual translated PDF.
        device: Inference device ("auto", "gpu", or "cpu"). Defaults to the device used in `extract_segments`.
    """
    if session_id not in SESSIONS:
        return f"Error: Unknown session_id '{session_id}'."

    session = SESSIONS[session_id]
    pdf_file = Path(session["pdf_path"])
    session_dir = Path(session["session_dir"])

    out_path = Path(output_dir).resolve() if output_dir else pdf_file.parent
    out_path.mkdir(parents=True, exist_ok=True)

    # If user leaves device="auto", inherit the explicit device from extract_segments if one was set
    device_pref = _normalize_device(
        device if device != "auto" else session.get("device", "auto"),
        session.get("device", "auto"),
    )

    assemble_log = session_dir / "assemble.log"
    session["assemble_status"] = "running"
    session["assemble_error"] = None
    session["assemble_start_time"] = time.time()
    session["assemble_log"] = str(assemble_log)
    session["output_dir"] = str(out_path)
    session["assemble_device"] = device_pref

    def _run_assembly():
        cmd = [
            BABELDOC_PYTHON,
            str(ENGINE_SCRIPT),
            "--mode",
            "assemble",
            "--pdf-path",
            str(pdf_file),
            "--session-dir",
            str(session_dir),
            "--output-dir",
            str(out_path),
            "--lang-in",
            session["lang_in"],
            "--lang-out",
            session["lang_out"],
            "--device",
            device_pref,
        ]
        if session["pages"] and session["pages"].strip():
            cmd.extend(["--pages", session["pages"].strip()])
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
                session["assemble_pid"] = proc.pid
                proc.wait()

            stem = pdf_file.stem
            generated = list(out_path.glob(f"{stem}*mono*.pdf")) + list(
                out_path.glob(f"{stem}*dual*.pdf")
            )

            if proc.returncode != 0 and not generated:
                log_tail = ""
                if assemble_log.exists():
                    log_tail = assemble_log.read_text(encoding="utf-8", errors="replace")[-2000:]
                session["assemble_status"] = "failed"
                session["assemble_error"] = (
                    f"Process exited with code {proc.returncode}.\nLog tail:\n{log_tail}"
                )
            else:
                session["assemble_status"] = "completed"
                session["assemble_elapsed"] = round(
                    time.time() - session["assemble_start_time"], 2
                )
                session["generated_files"] = [str(p) for p in generated]
        except Exception as e:
            session["assemble_status"] = "failed"
            session["assemble_error"] = str(e)

    thread = threading.Thread(target=_run_assembly, daemon=True)
    thread.start()

    return json.dumps(
        {
            "session_id": session_id,
            "status": "running",
            "device": device_pref,
            "engine": "fast-in-memory-v2",
            "message": (
                f"PDF assembly started in background (device={device_pref}). "
                "Please call `check_assembly_status(session_id)` to poll progress."
            ),
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def check_assembly_status(session_id: str, wait_seconds: int = 10) -> str:
    """
    Poll the status of a background PDF assembly job started by `assemble_translated`.
    Wait up to `wait_seconds` (capped at 25s) for completion.
    """
    if session_id not in SESSIONS:
        return f"Error: Unknown session_id '{session_id}'."

    session = SESSIONS[session_id]
    if "assemble_status" not in session:
        return f"Error: `assemble_translated` has not been called for session '{session_id}'."

    wait_seconds = min(max(1, wait_seconds), 25)
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if session["assemble_status"] in ("completed", "failed"):
            break
        time.sleep(0.5)

    if session["assemble_status"] == "running":
        elapsed = int(time.time() - session["assemble_start_time"])
        log_tail = ""
        log_file = Path(session["assemble_log"])
        if log_file.exists():
            lines = (
                log_file.read_text(encoding="utf-8", errors="replace")
                .strip()
                .splitlines()
            )
            if lines:
                log_tail = lines[-1][-200:]
        return json.dumps(
            {
                "session_id": session_id,
                "status": "running",
                "device": session.get("assemble_device", "auto"),
                "elapsed_seconds": elapsed,
                "latest_log": log_tail,
                "message": "Still typesetting and rendering PDF... Please call `check_assembly_status` again.",
            },
            ensure_ascii=False,
            indent=2,
        )

    if session["assemble_status"] == "failed":
        return json.dumps(
            {
                "session_id": session_id,
                "status": "failed",
                "device": session.get("assemble_device", "auto"),
                "error": session["assemble_error"],
            },
            ensure_ascii=False,
            indent=2,
        )

    return json.dumps(
        {
            "session_id": session_id,
            "status": "completed",
            "device": session.get("assemble_device", "auto"),
            "elapsed_seconds": session.get("assemble_elapsed"),
            "output_dir": session["output_dir"],
            "generated_files": session.get("generated_files", []),
        },
        ensure_ascii=False,
        indent=2,
    )


def main():
    mcp.run()


if __name__ == "__main__":
    main()
