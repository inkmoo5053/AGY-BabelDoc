---
name: babeldoc-translate
description: 使用 BabelDOC / pdf2zh-next-mcp 对 PDF 学术论文进行高保真双语/单语排版翻译，全程直接使用 Antigravity 内置模型额度。
---

# BabelDOC 论文与课件排版翻译指南

本技能利用 `babeldoc-translator` MCP 工具与 Antigravity 内置大模型能力，实现对 PDF 文档的高保真版面还原翻译。系统已内置 Windows 平台环境自愈、版面 NMS 重合消除、无调试边框渲染等完整保障机制。

## 输入文档格式注意事项
- **原生支持 PDF**：BabelDOC 核心专为 PDF 矢量与版面排版设计。
- **PPTX 课件处理规范**：若用户传入的是 `.pptx` 课件文件，优先检查同目录下是否已存在同名 `.pdf`；若不存在，直接调用 PowerShell 的 PowerPoint COM 接口静默无损导出为同名 `.pdf` 后立即启动分段提取流程，无需来回向用户发起交互确认，避免不必要的耗时。

## 核心流程（解耦三步法）

1. **提取分段（Extract）**：
   调用 MCP 工具 `extract_segments(file="<PDF绝对路径>", lang_in="en", lang_out="zh", pages="<可选页码>", device="auto")`。
   - `device` 加速设备选项：
     - `"auto"`（默认推荐）：自动检测并优先调用本地 NVIDIA 独立 GPU 进行视觉版面分析加速（速度提升约 6.5 倍），若 GPU 不可用则自动无缝降级至 CPU。
     - `"gpu"`：强制启用独立 GPU（DirectML / CUDA）硬件加速。
     - `"cpu"`：仅使用 CPU 运算。
   - 工具会异步在本地启动版面解析与公式保护（LaTeX 公式会被替换为 `{v0}`, `{v1}` 等占位符）。
   - 调用 `check_extraction_status(session_dir)` 轮询直至状态变为 `"extracted"`。
   - 提取完成后，状态返回中将包含待翻译的文本分段列表 `segments`。

2. **内置 AI 翻译（Translate via Antigravity Quota）**：
   - **由 Antigravity 当前会话内置模型直接进行高质量翻译**，无需调用外部付费 API。
   - **学术翻译铁律**：
     - **占位符保护**：严禁修改或删除 `{v0}`, `{v1}`, `{v2}` 等公式和变量占位符。
     - **严谨客观**：保持学术论文/专业课件客观、严谨、专业的中文表达风格。
     - **格式与引用**：参考文献（References/Bibliography）部分不作翻译，保留原引用格式。
     - **上下文语义**：遇到分页截断或换行的长句，结合前后段落保持上下文语义连贯。
     - **1:1 严格对齐**：输出的译文数组长度必须与输入的 `segments` 数组长度严格一致。

3. **保存与重排版（Assemble）**：
   - 将翻译好的字符串数组序列化为 JSON 字符串，调用 `save_translated_segments(session_dir=..., segments_json="[\"译文1\", \"译文2\", ...]")` 保存译文分段。
   - 调用 `assemble_translated(session_dir=..., device="auto")` 启动本地矢量排版重构（若未指定 device，则自动沿用提取阶段的设备设置）。
   - 调用 `check_assembly_status(session_dir)` 等待生成完毕。
   - 生成完毕后，最终输出中英双语对照版（`-dual.pdf`）以及单语版（`-mono.pdf`），文件自动保存在原 PDF 所在目录下。
