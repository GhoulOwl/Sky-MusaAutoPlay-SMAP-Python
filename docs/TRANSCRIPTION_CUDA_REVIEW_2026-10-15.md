# 默认混合后端、Windows CUDA 与草稿排序验证

日期：2026-10-15。执行环境：macOS arm64，Apple Silicon；没有 Windows / NVIDIA GPU 可用。

## 改动

- AI 扒谱新窗口默认 Demucs＋GAME＋MuScriptor，MuScriptor small 默认、medium 可选。
  打开已有草稿仍还原其保存时的后端；钢琴和弦、小提琴主旋律、延音和休止规则保持原逻辑。
- Windows 组件构建默认使用 PyTorch 2.8.0 + cu128，混合组件搭配同版本 TorchAudio。
  [官方 PyTorch 2.8.0 发布说明](https://pytorch.org/get-started/previous-versions/#v280)提供对应 Windows CUDA 12.8 发行包。
  `--torch-runtime cpu` 可构建独立 CPU 变体；CI 是否有 GPU 不影响打包选择。
- Demucs、GAME、MuScriptor 三个模型阶段均传入设备选择。自动模式先检查 CUDA 可用性，
  再执行小矩阵运算和同步，成功后使用 GPU，否则记录原因并使用 CPU。
  macOS 自动模式保持 Demucs/GAME CPU、MuScriptor MPS。显式 CPU 不触发 CUDA 探测。
  推理中显存不足明确失败并提示重试，不把部分结果当作完整曲谱，不静默更换模型。
- Hybrid 组件升至 3.1.0，MuScriptor 组件升至 2.1.0。仍支持 3.0.0 / 2.0.0；
  老混合组件的 Demucs/GAME 仍标识为 CPU，Windows 界面提示升级才能启用全阶段 CUDA。
  组件清单和设备策略进入缓存身份，CPU/CUDA 变体不会因入口程序相同而混用缓存。
- 新草稿固定保存 `created`，列表按其倒序排列；`updated` 继续记录最近编辑。
  编辑、重命名、重新打开不会改变排序，另建候选版本有自己的生成时间。
  旧草稿优先用目录创建时间推定，不可用时冻结迁移前的保存时间，并标记为推定。
  迁移保留文件修改时间，避免干扰派生音频的 LRU 清理。

## 验证结果

| 项目 | 结果 | 范围 |
| --- | --- | --- |
| 完整测试集 | 332 passed，16.34 秒 | 包含现有编配、延音、休止、同音重奏、精修、草稿、界面和组件安装回归 |
| 最后一次 CLI 单阶段设备覆盖调整 | 20 passed | 混合阶段设备继承、显式覆盖和分层缓存复用 |
| Ruff / diff 检查 | 通过 | `ruff check src tests scripts`、`git diff --check` |
| CUDA 选择单元测试 | 通过 | 可用 GPU、无 GPU、内核不兼容、驱动错误、显式 CPU 和原有 MPS 策略；模拟 torch 接口 |
| 混合阶段集成测试 | 通过 | 模拟模型输出，实际运行任务命令传参、阶段缓存、auto/CPU 隔离和旧组件兼容逻辑 |
| Windows Python 3.12 依赖解析 | 通过 | hybrid 66 项、muscriptor 55 项依赖解析；未在 Windows 安装执行 |
| Windows CUDA wheel 解析 | 通过 | 官方索引解析 torch / torchaudio 2.8.0+cu128；未在本机下载或加载 Windows DLL |
| 本机 Hybrid / MuScriptor `--check` | 通过 | Hybrid 3.1.0 / MuScriptor 2.1.0，PyTorch 2.8.0；CUDA 不可用，MPS 可用 |
| 真实 Demucs 短片段分轨 | 通过 | 1 秒合成正弦波，auto → CPU；加载约 0.11 秒，推理约 1.54 秒；原曲和四轨均为有效 44.1 kHz 双声道 |
| 应用启动与界面截图 | 通过 | 新窗口显示混合默认、small 默认、自动选择设备 |
| 草稿生成时间 | 通过 | 编辑、重命名、重新打开、另建版本、旧格式迁移及 LRU 时间保护 |

本机证据保存在 `artifacts/default-cuda-drafts-2026-10-15/`：
`transcription.png`、`separation-smoke/separation.json`、`separation-smoke/worker.jsonl` 及对应 WAV。
短合成音频只验证运行链路和输出完整性，不用于评价真实歌曲准确度或推断 Windows GPU 性能。

## Windows 待验收范围与使用

当前已完成源码、Windows CUDA 环境准备与构建流程，没有在本机生成 Windows 安装包，
也没有执行 Windows 真机 GPU 推理。下一次 Windows 构建应检查冻结组件能启动、CUDA DLL 完整，
再在 NVIDIA GPU 机器上确认三个阶段日志的实际设备、整曲耗时和显存峰值；无 GPU 机器应能回退到 CPU。

在 Windows x64 上运行 `uv run python scripts/prepare_hybrid.py --package`，
产物为 `AutoSkyPlay-Hybrid-3.1.0-windows-x64-cu128.zip`。主程序导入该包后安装 GAME 和 MuScriptor 模型，
选择「自动选择加速设备」即可。独立原混音组件通过 `scripts/prepare_muscriptor.py --package` 构建。
安装旧版 CPU 组件不会自动变成 CUDA 组件，需要导入新版包。

本轮不改变音乐识别或编配算法，没有重新运行三首真实歌曲的效果对比。
不同设备可能产生浮点差异，Windows GPU 的效果一致性仍需随真机验收核对。
