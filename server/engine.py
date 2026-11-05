from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import pickle
import re
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("babeldoc_fast_engine")


def get_best_dml_device_id() -> int:
    """Detect NVIDIA discrete GPU adapter index via Windows DXGI enumeration."""
    if "BABELDOC_DML_DEVICE_ID" in os.environ:
        try:
            return int(os.environ["BABELDOC_DML_DEVICE_ID"])
        except ValueError:
            pass
    try:
        import ctypes
        from ctypes import POINTER, Structure, c_size_t, c_uint, c_void_p, c_wchar, wintypes

        class LUID(Structure):
            _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

        class DXGI_ADAPTER_DESC1(Structure):
            _fields_ = [
                ("Description", c_wchar * 128),
                ("VendorId", wintypes.UINT),
                ("DeviceId", wintypes.UINT),
                ("SubSysId", wintypes.UINT),
                ("Revision", wintypes.UINT),
                ("DedicatedVideoMemory", c_size_t),
                ("DedicatedSystemMemory", c_size_t),
                ("SharedSystemMemory", c_size_t),
                ("AdapterLuid", LUID),
                ("Flags", wintypes.UINT),
            ]

        class GUID(Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", wintypes.BYTE * 8),
            ]

        IID_IDXGIFactory1 = GUID(
            0x770AAE78,
            0xF26F,
            0x4DBA,
            (wintypes.BYTE * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87),
        )
        dxgi = ctypes.windll.dxgi
        pFactory = c_void_p()
        if dxgi.CreateDXGIFactory1(ctypes.byref(IID_IDXGIFactory1), ctypes.byref(pFactory)) == 0:
            vtable = ctypes.cast(pFactory.value, POINTER(POINTER(c_void_p))).contents
            EnumAdapters1 = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, c_uint, POINTER(c_void_p))(vtable[12])
            for i in range(8):
                pAdapter = c_void_p()
                if EnumAdapters1(pFactory, i, ctypes.byref(pAdapter)) != 0:
                    break
                adapter_vtable = ctypes.cast(pAdapter.value, POINTER(POINTER(c_void_p))).contents
                GetDesc1 = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, POINTER(DXGI_ADAPTER_DESC1))(
                    adapter_vtable[10]
                )
                desc = DXGI_ADAPTER_DESC1()
                if GetDesc1(pAdapter, ctypes.byref(desc)) == 0:
                    if desc.VendorId == 0x10DE or "nvidia" in desc.Description.lower():
                        ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(adapter_vtable[2])(pAdapter)
                        ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(vtable[2])(pFactory)
                        return i
                ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(adapter_vtable[2])(pAdapter)
            ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(vtable[2])(pFactory)
    except Exception:
        pass
    return 1


def configure_onnx_runtime(device: str) -> str:
    """Configure ONNX Runtime providers according to user device preference ('auto', 'gpu', 'cpu')."""
    import onnxruntime as ort

    device_norm = (device or "auto").strip().lower()
    os.environ["BABELDOC_DEVICE"] = device_norm

    orig_init = ort.InferenceSession.__init__
    if getattr(ort.InferenceSession.__init__, "_babeldoc_patched", False):
        return device_norm

    dml_dev_id = get_best_dml_device_id()
    avail = ort.get_available_providers()

    def patched_init(self, path_or_bytes, sess_options=None, providers=None, **kwargs):
        pref = os.environ.get("BABELDOC_DEVICE", "auto").strip().lower()
        if pref == "cpu":
            target_providers = ["CPUExecutionProvider"]
        else:
            if "DmlExecutionProvider" in avail:
                target_providers = [
                    ("DmlExecutionProvider", {"device_id": dml_dev_id}),
                    "CPUExecutionProvider",
                ]
            elif "CUDAExecutionProvider" in avail:
                target_providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
            else:
                target_providers = ["CPUExecutionProvider"]
        return orig_init(self, path_or_bytes, sess_options=sess_options, providers=target_providers, **kwargs)

    patched_init._babeldoc_patched = True
    ort.InferenceSession.__init__ = patched_init
    logger.info(
        f"ONNX Runtime configured: device='{device_norm}', available_providers={avail}, dml_device_id={dml_dev_id}"
    )
    return device_norm


def install_fast_layout_handler():
    """Accelerate OnnxModel.handle_document by parallelizing CPU image preprocessing across threads
    while keeping GPU/CPU ONNX inference fed without idle gaps."""
    import cv2
    import numpy as np
    from babeldoc.docvision.base_doclayout import YoloResult
    from babeldoc.docvision.doclayout import OnnxModel
    from babeldoc.format.pdf.document_il.utils.mupdf_helper import get_no_rotation_img

    if getattr(OnnxModel.handle_document, "_fast_patched", False):
        return

    def fast_handle_document(self, pages, mupdf_doc, translate_config, save_debug_image):
        total_pages = len(pages)
        if total_pages == 0:
            return

        # Step 1: Render page pixmaps sequentially (MuPDF C-API is fast and requires lock)
        raw_images = []
        for page in pages:
            translate_config.raise_if_cancelled()
            with self.lock:
                pix = get_no_rotation_img(mupdf_doc[page.page_number])
            img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3)[:, :, ::-1]
            raw_images.append((page, img))

        # Step 2: Parallelize OpenCV resize + pad + float32 normalization across CPU cores
        target_imgsz = 1024

        def preprocess_one(item):
            page, img = item
            orig_h, orig_w = img.shape[:2]
            pix_resized = self.resize_and_pad_image(img, new_shape=target_imgsz)
            pix_chw = np.transpose(pix_resized, (2, 0, 1)).astype(np.float32) / 255.0
            batch_input = np.expand_dims(pix_chw, axis=0)
            return page, img, (orig_h, orig_w), batch_input

        max_workers = min(8, os.cpu_count() or 4)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for page, img, orig_shape, batch_input in pool.map(preprocess_one, raw_images):
                translate_config.raise_if_cancelled()
                new_h, new_w = batch_input.shape[2:]
                batch_preds = self.model.run(None, {"images": batch_input})[0]
                preds = batch_preds[0]
                preds = preds[preds[..., 4] > 0.25]
                if len(preds) > 0:
                    preds[..., :4] = self.scale_boxes(
                        (new_h, new_w),
                        preds[..., :4],
                        orig_shape,
                    )
                result = YoloResult(boxes_data=preds, names=self._names)
                save_debug_image(img, result, page.page_number + 1)
                yield page, result

    fast_handle_document._fast_patched = True
    OnnxModel.handle_document = fast_handle_document


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())


def _strip_bullets(s: str) -> str:
    s = re.sub(r"^[\s\u2022\u25aa\u25cf\u25e6\u25b8\u2023\u2043\uf0b7\uf0a7\-\*\·]+", "", s)
    s = re.sub(r"([\n\r]+)[\s\u2022\u25aa\u25cf\u25e6\u25b8\u2023\u2043\uf0b7\uf0a7\-\*\·]+", r"\1", s)
    return _norm_ws(s)


def _strip_all_non_alnum(s: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", s).lower()


class InMemoryCollectorTranslator:
    """In-process translator for Pass 1 (Extraction).
    Collects all translatable segments in memory with zero subprocess overhead."""

    def __init__(self, lang_in: str = "en", lang_out: str = "zh", segments_file: Path | None = None):
        self.lang_in = lang_in
        self.lang_out = lang_out
        self.name = "in-memory-collector"
        self.segments: list[dict] = []
        self.segments_file = segments_file

    def translate(self, text: str, ignore_cache: bool = False, rate_limit_params: dict | None = None) -> str:
        idx = len(self.segments)
        self.segments.append({"id": idx, "source": text, "translated": ""})
        # Periodically flush so check_extraction_status can report live progress
        if self.segments_file and (len(self.segments) % 10 == 0):
            self.flush()
        return text

    def llm_translate(self, text: str, ignore_cache: bool = False, rate_limit_params: dict | None = None) -> str:
        return self.translate(text, ignore_cache=ignore_cache, rate_limit_params=rate_limit_params)

    def do_llm_translate(self, text: str | None, rate_limit_params: dict | None = None):
        raise NotImplementedError

    def get_formular_placeholder(self, placeholder_id: int):
        return f"{{v{placeholder_id}}}", rf"{{\s*v\s*{placeholder_id}\s*}}"

    def get_rich_text_left_placeholder(self, placeholder_id: int):
        return f"<style id='{placeholder_id}'>", rf"<\s*style\s+id\s*=\s*'{placeholder_id}'\s*>"

    def get_rich_text_right_placeholder(self, placeholder_id: int):
        return "</style>", r"<\s*\/\s*style\s*>"

    def flush(self):
        if self.segments_file:
            tmp_file = self.segments_file.with_suffix(".json.tmp")
            tmp_file.write_text(
                json.dumps(self.segments, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_file.replace(self.segments_file)


class InMemoryLookupTranslator:
    """In-process translator for Pass 2 (Assembly).
    Matches source text against translated segments in memory with multi-level fuzzy fallback."""

    def __init__(self, segments: list[dict], lang_in: str = "en", lang_out: str = "zh"):
        self.lang_in = lang_in
        self.lang_out = lang_out
        self.name = "in-memory-lookup"
        self.exact_map: dict[str, str] = {}
        self.ws_map: dict[str, str] = {}
        self.bullet_map: dict[str, str] = {}
        self.alnum_map: dict[str, str] = {}
        self.ordered_segments = segments
        self.call_idx = 0
        self.hit_count = 0
        self.miss_count = 0

        for item in segments:
            src = item.get("source", "")
            dst = item.get("translated", "")
            if src and dst:
                self.exact_map[src] = dst
                self.ws_map[_norm_ws(src)] = dst
                self.bullet_map[_strip_bullets(src)] = dst
                alnum_key = _strip_all_non_alnum(src)
                if alnum_key:
                    self.alnum_map[alnum_key] = dst

    def translate(self, text: str, ignore_cache: bool = False, rate_limit_params: dict | None = None) -> str:
        idx = self.call_idx
        self.call_idx += 1

        # 1. Exact match
        if text in self.exact_map:
            self.hit_count += 1
            return self.exact_map[text]

        # 2. Normalized whitespace match
        ws_key = _norm_ws(text)
        if ws_key in self.ws_map:
            self.hit_count += 1
            return self.ws_map[ws_key]

        # 3. Bullet-stripped match
        b_key = _strip_bullets(text)
        if b_key in self.bullet_map:
            self.hit_count += 1
            return self.bullet_map[b_key]

        # 4. Alphanumeric-only match
        a_key = _strip_all_non_alnum(text)
        if a_key and a_key in self.alnum_map:
            self.hit_count += 1
            return self.alnum_map[a_key]

        # 5. Positional index match (when IR cache guarantees identical traversal order)
        if idx < len(self.ordered_segments):
            cand = self.ordered_segments[idx]
            if cand.get("translated"):
                self.hit_count += 1
                return cand["translated"]

        self.miss_count += 1
        return text

    def llm_translate(self, text: str, ignore_cache: bool = False, rate_limit_params: dict | None = None) -> str:
        return self.translate(text, ignore_cache=ignore_cache, rate_limit_params=rate_limit_params)

    def do_llm_translate(self, text: str | None, rate_limit_params: dict | None = None):
        raise NotImplementedError

    def get_formular_placeholder(self, placeholder_id: int):
        return f"{{v{placeholder_id}}}", rf"{{\s*v\s*{placeholder_id}\s*}}"

    def get_rich_text_left_placeholder(self, placeholder_id: int):
        return f"<style id='{placeholder_id}'>", rf"<\s*style\s+id\s*=\s*'{placeholder_id}'\s*>"

    def get_rich_text_right_placeholder(self, placeholder_id: int):
        return "</style>", r"<\s*\/\s*style\s*>"


def build_translation_config(
    input_pdf_path: Path,
    output_dir: Path,
    working_dir: Path,
    translator,
    lang_in: str,
    lang_out: str,
    pages: str | None,
    no_dual: bool = False,
    no_mono: bool = False,
    load_layout_model: bool = True,
):
    from babeldoc.docvision.doclayout import DocLayoutModel
    from babeldoc.format.pdf.translation_config import TranslationConfig, WatermarkOutputMode
    from babeldoc.progress_monitor import ProgressMonitor
    from babeldoc.format.pdf.high_level import TRANSLATE_STAGES

    doc_layout_model = DocLayoutModel.load_onnx() if load_layout_model else object()
    config = TranslationConfig(
        input_file=str(input_pdf_path),
        font=None,
        pages=pages,
        output_dir=str(output_dir),
        doc_layout_model=doc_layout_model,
        translator=translator,
        debug=False,
        lang_in=lang_in,
        lang_out=lang_out,
        no_dual=no_dual,
        no_mono=no_mono,
        qps=1000,
        formular_font_pattern=None,
        formular_char_pattern=None,
        split_short_lines=False,
        short_line_split_factor=0.8,
        use_rich_pbar=False,
        dual_translate_first=False,
        disable_rich_text_translate=True,
        enhance_compatibility=False,
        report_interval=0.5,
        min_text_length=5,
        watermark_output_mode=WatermarkOutputMode.NoWatermark,
        split_strategy=None,
        pool_max_workers=1,
        auto_extract_glossary=False,
        working_dir=str(working_dir),
    )
    pm = ProgressMonitor(TRANSLATE_STAGES)
    config.progress_monitor = pm
    return config


def parse_pdf_to_pre_translation_ir(translation_config):
    """Run BabelDOC stages 1 through 6 (up to StylesAndFormulas) and return (docs, mediabox_data, saved_pdf_path)."""
    from pymupdf import Document
    from babeldoc.const import close_process_pool
    from babeldoc.format.pdf.document_il.midend.detect_scanned_file import DetectScannedFile
    from babeldoc.format.pdf.document_il.midend.layout_parser import LayoutParser
    from babeldoc.format.pdf.document_il.midend.paragraph_finder import ParagraphFinder
    from babeldoc.format.pdf.document_il.midend.styles_and_formulas import StylesAndFormulas
    from babeldoc.format.pdf.document_il.midend.table_parser import TableParser
    from babeldoc.format.pdf.high_level import (
        check_cid_char,
        fix_filter,
        fix_media_box,
        fix_null_page_content,
        fix_null_xref,
        safe_save,
    )
    from babeldoc.format.pdf.new_parser.native_parse import (
        parse_prepared_pdf_with_new_parser_to_legacy_ir,
    )

    original_pdf_path = translation_config.input_file
    temp_pdf_path = Path(translation_config.get_working_file_path("input.pdf"))
    doc_pdf2zh = Document(original_pdf_path)
    safe_save(doc_pdf2zh, temp_pdf_path)

    try:
        try:
            _ = fix_null_page_content(doc_pdf2zh)
            fix_filter(doc_pdf2zh)
            fix_null_xref(doc_pdf2zh)
        except Exception:
            logger.exception("auto fix failed, please check the pdf file")

        mediabox_data = fix_media_box(doc_pdf2zh)
        safe_save(doc_pdf2zh, temp_pdf_path)

        docs = parse_prepared_pdf_with_new_parser_to_legacy_ir(
            temp_pdf_path,
            config=translation_config,
            doc_pdf=doc_pdf2zh,
        )

        if not translation_config.skip_scanned_detection:
            DetectScannedFile(translation_config).process(
                docs, temp_pdf_path, mediabox_data
            )

        docs = LayoutParser(translation_config).process(docs, doc_pdf2zh)
        close_process_pool()

        if translation_config.table_model:
            docs = TableParser(translation_config).process(docs, doc_pdf2zh)

        ParagraphFinder(translation_config).process(docs)
        StylesAndFormulas(translation_config).process(docs)
    finally:
        doc_pdf2zh.close()

    return docs, mediabox_data, temp_pdf_path


def run_extract(
    pdf_path: Path,
    session_dir: Path,
    lang_in: str,
    lang_out: str,
    pages: str | None,
    device: str,
):
    t0 = time.perf_counter()
    device_used = configure_onnx_runtime(device)
    install_fast_layout_handler()

    session_dir.mkdir(parents=True, exist_ok=True)
    segments_file = session_dir / "segments.json"
    segments_file.write_text("[]", encoding="utf-8")

    working_dir = session_dir / "work_extract"
    working_dir.mkdir(parents=True, exist_ok=True)
    dummy_out = session_dir / "out_extract"
    dummy_out.mkdir(parents=True, exist_ok=True)

    collector = InMemoryCollectorTranslator(
        lang_in=lang_in,
        lang_out=lang_out,
        segments_file=segments_file,
    )
    config = build_translation_config(
        input_pdf_path=pdf_path,
        output_dir=dummy_out,
        working_dir=working_dir,
        translator=collector,
        lang_in=lang_in,
        lang_out=lang_out,
        pages=pages,
    )

    logger.info(f"[Pass 1: Extract] Starting fast extraction for '{pdf_path.name}' (device={device_used})...")
    docs, mediabox_data, temp_pdf_path = parse_pdf_to_pre_translation_ir(config)
    t_parsed = time.perf_counter()
    logger.info(f"[Pass 1: Extract] PDF & Layout parsing completed in {t_parsed - t0:.2f}s.")

    # Save a clean copy of the prepared PDF and pickled IR snapshot BEFORE translation mutates `docs`
    cached_pdf_path = session_dir / "prepared_clean.pdf"
    shutil.copy2(temp_pdf_path, cached_pdf_path)
    temp_pdf_path.unlink(missing_ok=True)

    ir_cache_path = session_dir / "ir_cache.pkl"
    try:
        with ir_cache_path.open("wb") as f:
            pickle.dump(
                {
                    "docs": docs,
                    "mediabox_data": mediabox_data,
                    "lang_in": lang_in,
                    "lang_out": lang_out,
                    "pages": pages,
                },
                f,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
        logger.info(f"[Pass 1: Extract] Saved IR cache snapshot to {ir_cache_path.name} ({ir_cache_path.stat().st_size / (1024*1024):.1f} MB).")
    except Exception as e:
        logger.warning(f"[Pass 1: Extract] Could not pickle IR cache ({e}); Pass 2 will re-parse if needed.")
        ir_cache_path.unlink(missing_ok=True)

    # Run ILTranslator in-memory to collect segments (takes < 0.05s, 0 subprocess spawns!)
    from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator

    ILTranslator(collector, config).translate(docs)
    collector.flush()

    shutil.rmtree(working_dir, ignore_errors=True)
    shutil.rmtree(dummy_out, ignore_errors=True)

    elapsed = time.perf_counter() - t0
    logger.info(
        f"[Pass 1: Extract] Done! Extracted {len(collector.segments)} segments in {elapsed:.2f}s (skipped redundant Pass 1 PDF rendering)."
    )


def run_assemble(
    pdf_path: Path,
    session_dir: Path,
    output_dir: Path,
    lang_in: str,
    lang_out: str,
    pages: str | None,
    no_dual: bool,
    no_mono: bool,
    device: str,
):
    t0 = time.perf_counter()
    device_used = configure_onnx_runtime(device)
    install_fast_layout_handler()

    segments_file = session_dir / "segments.json"
    if not segments_file.exists():
        raise FileNotFoundError(f"segments.json not found in {session_dir}")

    segments = json.loads(segments_file.read_text(encoding="utf-8"))
    lookup = InMemoryLookupTranslator(segments=segments, lang_in=lang_in, lang_out=lang_out)

    output_dir.mkdir(parents=True, exist_ok=True)
    working_dir = session_dir / "work_assemble"
    working_dir.mkdir(parents=True, exist_ok=True)

    ir_cache_path = session_dir / "ir_cache.pkl"
    cached_pdf_path = session_dir / "prepared_clean.pdf"
    docs = None
    mediabox_data = None
    temp_pdf_path = None

    if ir_cache_path.exists() and cached_pdf_path.exists():
        try:
            logger.info("[Pass 2: Assemble] Loading pre-parsed IR snapshot from Pass 1 cache...")
            with ir_cache_path.open("rb") as f:
                cache_obj = pickle.load(f)
            if cache_obj.get("pages") == pages:
                docs = cache_obj["docs"]
                mediabox_data = cache_obj["mediabox_data"]
                fd, tmp_str = tempfile.mkstemp(suffix=".pdf", prefix="babeldoc_clean_")
                os.close(fd)
                temp_pdf_path = Path(tmp_str)
                shutil.copy2(cached_pdf_path, temp_pdf_path)
                logger.info(
                    f"[Pass 2: Assemble] IR cache loaded in {time.perf_counter() - t0:.2f}s! Skipped ONNX model load & duplicate layout parsing."
                )
        except Exception as e:
            logger.warning(f"[Pass 2: Assemble] Failed to load IR cache ({e}), falling back to full parse.")
            docs = None

    config = build_translation_config(
        input_pdf_path=pdf_path,
        output_dir=output_dir,
        working_dir=working_dir,
        translator=lookup,
        lang_in=lang_in,
        lang_out=lang_out,
        pages=pages,
        no_dual=no_dual,
        no_mono=no_mono,
        load_layout_model=(docs is None),
    )

    if docs is None:
        logger.info(f"[Pass 2: Assemble] Running full parse for '{pdf_path.name}' (device={device_used})...")
        docs, mediabox_data, temp_pdf_path = parse_pdf_to_pre_translation_ir(config)

    try:
        import pymupdf
        from babeldoc.format.pdf.document_il.backend.pdf_creater import PDFCreater
        from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
        from babeldoc.format.pdf.document_il.midend.typesetting import Typesetting

        t_trans0 = time.perf_counter()
        ILTranslator(lookup, config).translate(docs)
        logger.info(
            f"[Pass 2: Assemble] In-memory translation injection finished in {time.perf_counter() - t_trans0:.3f}s "
            f"(hits={lookup.hit_count}, misses={lookup.miss_count})."
        )

        t_type0 = time.perf_counter()
        Typesetting(config).typesetting_document(docs)
        logger.info(f"[Pass 2: Assemble] Typesetting finished in {time.perf_counter() - t_type0:.2f}s.")

        t_write0 = time.perf_counter()
        pdf_creater = PDFCreater(
            str(temp_pdf_path),
            docs,
            config,
            mediabox_data,
        )
        result = pdf_creater.write(config)

        config.progress_monitor.on_finish()
        elapsed = time.perf_counter() - t0
        logger.info(
            f"[Pass 2: Assemble] PDF rendering finished in {time.perf_counter() - t_write0:.2f}s. Total Pass 2 time: {elapsed:.2f}s."
        )
        logger.info(
            f"[Pass 2: Assemble] Outputs: mono={getattr(result, 'mono_pdf_path', None)}, dual={getattr(result, 'dual_pdf_path', None)}"
        )
    finally:
        if temp_pdf_path is not None:
            temp_pdf_path.unlink(missing_ok=True)
        shutil.rmtree(working_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="BabelDOC Fast In-Memory Engine")
    parser.add_argument("--mode", choices=["extract", "assemble"], required=True)
    parser.add_argument("--pdf-path", required=True)
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--lang-in", default="en")
    parser.add_argument("--lang-out", default="zh")
    parser.add_argument("--pages", default="")
    parser.add_argument("--no-dual", action="store_true")
    parser.add_argument("--no-mono", action="store_true")
    parser.add_argument("--device", default="auto", choices=["auto", "gpu", "cpu"])
    args = parser.parse_args()

    pdf_path = Path(args.pdf_path).resolve()
    session_dir = Path(args.session_dir).resolve()
    pages = args.pages.strip() if args.pages and args.pages.strip() else None

    if args.mode == "extract":
        run_extract(
            pdf_path=pdf_path,
            session_dir=session_dir,
            lang_in=args.lang_in,
            lang_out=args.lang_out,
            pages=pages,
            device=args.device,
        )
    elif args.mode == "assemble":
        output_dir = Path(args.output_dir).resolve() if args.output_dir else pdf_path.parent
        run_assemble(
            pdf_path=pdf_path,
            session_dir=session_dir,
            output_dir=output_dir,
            lang_in=args.lang_in,
            lang_out=args.lang_out,
            pages=pages,
            no_dual=args.no_dual,
            no_mono=args.no_mono,
            device=args.device,
        )


if __name__ == "__main__":
    main()
