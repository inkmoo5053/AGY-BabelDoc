---
name: babeldoc-translate
description: 使用 BabelDOC / pdf2zh-next-mcp 对 PDF 学术论文与课件进行高保真双语/单语排版翻译，全程直接使用 Antigravity 内置模型额度。
---

# BabelDOC 论文与课件排版翻译指南 (v2.0 极速内存架构)

本技能利用 `babeldoc-plugin_babeldoc-translator`（或 `babeldoc-translator`）MCP 工具与 Antigravity 内置大模型能力，实现对 PDF 文档的高保真版面还原翻译。系统已内置 DirectML 独显自动绑定、零子进程内存收集/回填、多线程图像预处理与 Pass 1 → Pass 2 的 IR 快照缓存。

> **效率铁律（免查 Schema 直接调用）**：
> 下方已提供全部 5 个 MCP 工具的完整参数签名与调用模板。**严禁在任务开始时再去调用 `read_file` 或 `run_command` 查看 `.gemini/antigravity/mcp/` 下的 `.json` Schema 文件**，请直接使用 `call_mcp_tool` 发起调用！

## 输入文档格式注意事项
- **原生支持 PDF**：BabelDOC 核心专为 PDF 矢量与版面排版设计。
- **PPTX 课件处理规范**：若用户传入的是 `.pptx` 课件文件，优先检查同目录下是否已存在同名 `.pdf`；若不存在，直接调用 PowerShell 的 PowerPoint COM 接口静默无损导出为同名 `.pdf` 后立即启动分段提取流程，无需来回向用户发起交互确认。

## 核心流程（解耦三步法 + 内存级 IR 快照加速）

### 1. 提取分段（Extract）
直接调用 `call_mcp_tool` 启动 `extract_segments`（`ServerName` 传 `"babeldoc-plugin_babeldoc-translator"`）：
```json
{
  "file": "<PDF绝对路径>",
  "output_dir": "<输出目录绝对路径，可选>",
  "lang_in": "en",
  "lang_out": "zh",
  "pages": "",
  "device": "auto"
}
```
- **`device` 加速设备选项（支持自由切换 GPU 或 CPU）**：
  - `"auto"`（默认推荐）：自动通过 DXGI 枚举优先调用本地 NVIDIA 独立显卡（`DmlExecutionProvider`, `device_id=1`）进行版面分析加速，若无独显则自动降级至 CPU。
  - `"gpu"`：强制启用独立 GPU 硬件加速。
  - `"cpu"`：强制仅使用 CPU 运算（独显显存与算力占用为 0%，但依然享受多核预处理、零子进程内存收集与 IR 缓存加速）。
- 随后调用 `check_extraction_status` 轮询直至返回的 `status` 为 `"extracted"`：
```json
{
  "session_dir": "<extract_segments返回的session_dir>",
  "offset": 0,
  "wait_seconds": 20
}
```
- 提取完成后，返回结果中的 `segments` 数组即为按顺序排列的待翻译源文本列表（同时在 `session_dir/segments.json` 中存有完整 JSON，可直接读取）。

### 2. 内置 AI 翻译（Translate via Antigravity Quota）
- **由 Antigravity 当前会话内置模型直接进行高质量翻译**，无需调用外部付费 API。
- **学术翻译铁律**：
  - **占位符保护**：严禁修改或删除 `{v1}`, `{v2}` 或 `<style id='1'>...</style>` 等公式和富文本占位符。
  - **严谨客观**：保持学术论文/专业课件客观、严谨、专业的中文表达风格。
  - **格式与引用**：参考文献（References/Bibliography）部分不作翻译，保留原引用格式。
  - **上下文语义**：遇到分页截断或换行的长句，结合前后段落保持上下文语义连贯。
  - **1:1 严格对齐**：输出的译文数组长度必须与输入的 `segments` 数组长度严格一致。

### 3. 保存与重排版（Assemble）
- 将翻译好的中文字符串列表序列化为 JSON 数组字符串，调用 `save_translated_segments`：
```json
{
  "session_dir": "<session_dir>",
  "segments_json": "[\"译文0\", \"译文1\", ...]"
}
```
  *(注：`segments_json` 也支持传 `[{"id": 0, "translated": "..."}]` 或 `{"0": "..."}`，并支持分批多次调用累加保存)*。
- 调用 `assemble_translated` 启动本地矢量排版重构（自动复用 Pass 1 的 `ir_cache.pkl` 快照，0 秒跳过重复版面识别，内存中毫秒级注入译文后直接生成 PDF）：
```json
{
  "session_dir": "<session_dir>",
  "output_dir": "<输出目录绝对路径，可选>",
  "device": "auto"
}
```
- 调用 `check_assembly_status` 等待生成完毕：
```json
{
  "session_dir": "<session_dir>",
  "wait_seconds": 20
}
```
- 当 `status` 变为 `"completed"` 后，`generated_files` 中即包含最终生成的中英双语对照版（`*.dual.pdf`）以及中文单语版（`*.mono.pdf`）。
