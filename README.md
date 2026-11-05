# AGY-BabelDoc (v2.0 Fast In-Memory Pipeline)

专为 Google Antigravity 打造的学术论文与复杂课件 PDF 双语/单语高保真排版翻译插件。

## 核心特性 (v2.0)

1. **进程内极速内存架构 (`fast-in-memory-v2`)**：
   - 彻底消除传统 `CLITranslator` 每段文本启动外部 `python.exe` 子进程的 IPC 通信瓶颈（提取与回填耗时从数分钟缩短至 `< 0.1 秒`）。
2. **Pass 1 → Pass 2 中间表示快照缓存 (`ir_cache.pkl`)**：
   - Pass 1 提取完分段后直接保存版面与公式结构快照，跳过冗余的首次 PDF 渲染；
   - Pass 2 组装时 `0.7 秒` 瞬间恢复版面缓存，彻底跳过二次加载 ONNX 模型与二次 YOLO 版面分析。
3. **NVIDIA 独显与 CPU 自由切换 (`device="auto" | "gpu" | "cpu"`)**：
   - `"auto"` / `"gpu"`：通过 Windows DXGI 自动识别并绑定 NVIDIA 独立显卡（`DmlExecutionProvider`），避免双显卡笔记本误绑核显。
   - `"cpu"`：强制使用 `CPUExecutionProvider`（0 MB 独显显存占用），同时依然享受多线程图像预处理与 IR 快照加速。
4. **平时零常驻显存占用**：
   - 闲置时仅保留轻量 MCP 通信壳（`0 MB` 显存占用），翻译任务结束后子进程立即释放全部显存与内存。

---

## 快速安装指南 (Windows)

### 1. 克隆插件至 Antigravity 全局插件目录
```powershell
git clone https://github.com/inkmoo5053/AGY-BabelDoc.git "$env:USERPROFILE\.gemini\config\plugins\babeldoc-plugin"
```

### 2. 初始化底层依赖环境（首次安装执行一次，或交由插件首次运行时自动自愈）
```powershell
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\.gemini\config\plugins\babeldoc-plugin\install.ps1"
```

### 3. 在 Antigravity 中启用
重启 Antigravity（或在 Customizations -> Plugins 页面开启 `BabelDOC 论文排版翻译`），即可直接通过 `@[plugin:babeldoc-plugin]` 调用翻译。
