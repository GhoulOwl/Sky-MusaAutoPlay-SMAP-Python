# AI 扒谱：本地混合引擎

默认使用 Demucs＋GAME＋MuScriptor：Demucs 分轨、GAME medium 识别人声、MuScriptor small 识别背景，
MuScriptor medium 可选；也可切换到 MuScriptor 原混音后端。音频和识别在本机处理，不上传到模型服务。
钢琴默认完整主旋律＋稀疏和弦，仍可选择稀疏单音伴奏；小提琴默认完整主旋律，单音伴奏可选。
不明确的旋律区域保留休止并标记待修正。生成结果是可编辑初稿。

## 安装与使用

1. 在扒谱窗口「组件管理」导入对应平台的 `AutoSkyPlay-Hybrid-3.1.0-<platform>[-cu128/-cpu].zip`。
   组件提供独立 Python/PyTorch/FFmpeg 环境和固定 Demucs 权重；主程序不加载 PyTorch。
   原混音后端使用独立 `AutoSkyPlay-MuScriptor-2.1.0-<platform>[-cu128/-cpu].zip`。
   从 GitHub Actions 下载的外层 ZIP 也可直接导入，无需先解压：程序校验其中组件 ZIP 的 SHA-256，
   再校验组件清单及全部文件。导入成功后会切换到对应后端并显示已安装的组件版本；
   同时保留已有模型和草稿。仍提示缺少模型时，按下一步安装模型即可。
2. 在主页面「设置 → 扒谱模型管理」安装 GAME medium，再下载 MuScriptor small（约 412 MB）或 medium（约 1.23 GB）。
   下载源固定为 ModelScope 的 `MuScriptor/muscriptor-small`、`MuScriptor/muscriptor-medium` 社区镜像，
   固定修订和 SHA256，支持取消、续传、重试、本地目录导入、单独删除。
3. 选择网易云歌曲，或在选歌弹窗「本地上传」选择音频/MIDI。原曲准备后显示真实波形，
   可拖动入点/出点选择范围。默认 30 秒，单次最多 10 分钟；仅有试听音频时明确限制范围。
4. 选择 small / medium 和自动加速 / CPU，点击开始扒谱。Windows 自动优先 CUDA，不可用时使用 CPU；
   macOS 保留 Demucs/GAME 使用 CPU、MuScriptor 使用 MPS（不可用时 CPU）。
   当前仅实测 macOS Apple Silicon；Windows CUDA 已加入构建和设备选择测试，尚需真机验收。
   选择模型仅影响下一次识别，不改写已有结果。
5. 修改音色、伴奏模式、最短音符、移调、BPM、节奏整理等参数可复用原始音符事件。
   MuScriptor 无逐音符校准置信度，因此隐藏旧引擎过滤阈值，也不重新执行旧式起音切分。
6. 可试听原曲、分离人声、背景和光遇谱，定位、循环、缩放；卷帘预览只读。
   原混音后端不分轨，没有分离文件时禁用相应试听选项。
7. 编辑曲谱后返回草稿，再保存到曲库。同名保存不覆盖曲库文件，调整已人工编辑的草稿会另建版本。
   右键草稿可重命名或删除。列表按首次生成时间倒序，编辑、重命名和重新打开不改变位置。
8. MuScriptor「导出原始 MIDI」保留所有模型声部、原始音高与时间，包含打击乐，使用固定力度。
   不含光遇音域映射、伴奏简化和人工编辑；乐器类别仍可能误识别。
9. MuScriptor 原混音音频草稿可点击「小节／片段精修…」。先选范围，生成候选，对照原版／候选／原曲试听，
   再接受为新版本。混合后端、旧引擎和 MIDI 暂不支持局部精修；源音频缺失时需先恢复。

## 小节精修与稀疏和弦

「参考小节」按已有拍点分组，默认 4 拍，可改为 2–12 拍及移动分组起点；它不等于自动识别拍号／重拍。
拍点可信度不足时使用时间选区，支持拖动标尺和输入精确秒数。建议一次 2–4 组，核心不超过 30 秒，
默认开启「寻找安全边界」，在两侧各带入最多 8 秒上下文，一次识别后向外各搜索最多 4 秒，
实际精修范围仍不超过 30 秒、不向内缩小。优先保留已安全的原选区，再寻找完整事件的起止／间隙；
最终还要通过映射后的长音、音尾和同键冲突检查，最多试算 6 组边界。
关闭安全边界时固定所选范围，按参考组长带入 2–8 秒上下文。
所有时间为当前草稿相对时间，调用源音频时加回原曲入点并扣除源文件起点。

候选生成使用已有 MuScriptor small / medium 本地权重，冻结原稿 BPM、拍点、节奏整理、全局移调和音色。
不引入新节奏模型。现有 2.0.0 组件兼容：即使旧组件额外估计了局部拍点，主程序也以冻结的原稿拍点覆盖，
局部估计不会用于改动原谱时钟。新 worker 可跳过这项重复分析；运行组件身份参与结果缓存键。

选区外和跨界音符逐字段保留，人工休止不会被填补，不按同键相邻规则合并真实重奏。候选不能自动裁掉
跨界长音来满足选区；无法安全对应的跨界事件、映射后的越界或持续旋律冲突会标记原因并禁止接受。
跨界同音事件必须一一对应，不能把多个重奏候选匹配到同一枚原稿长音。
窗口会显示原请求和实际精修范围，卷帘用灰虚线标原请求、金线标实际范围。
实际范围内会替换完整事件，范围外和仍跨界的原稿音符继续保护；接受前应试听扩大后的衔接。
搜索失败仍禁止接受，可丢弃后调整范围或模型重试。已有单音模式草稿的局部精修会冻结其原模式，
不会因全局推荐改为和弦而隐式换编配。

对照窗提供统一增益的原版／候选、仅主旋律、含前后衔接、循环、拖动和差异图。
拖动试听用带预热音尾的音频，保留进入片段的钢琴衰减与小提琴持续音。
绿色框表示候选独有音符，红色虚线表示原版独有音符；时值／键位变化按一删一增展示，不能据此判断准确率。
原曲仅作听审参考，固定增益不等于与合成音频响度匹配。

接受候选会先在临时目录生成完整新草稿及源文件引用，再原子发布；原草稿不覆盖。
失败不会暴露半成品，重复接受或发布后中断重试只返回同一个新版本。
原稿参数、谱面、原始事件或源音频改变会使待处理候选失效。关闭窗口保留候选，重新打开同一草稿可恢复。
待处理结果保存在 `transcription/refinements/<id>/`；接受后的草稿另存 `refinement-local-events.json`。
精修后的原始 MIDI 是原稿加局部事件的复合多轨结果，仍不包含光遇映射或手工谱面编辑。

钢琴默认「钢琴主旋律＋稀疏和弦」：从已有同时起音的伴奏候选中选择最多两枚低于旋律的键，
检查音域、大小三和弦包含关系、强音尾内的旋律冲突、伴奏组间距及自然衰减。
证据不足时退为单音或无伴奏，不凭空添加和弦音。主旋律与单音模式一致。
此模式仅针对内置钢琴采样验收；小提琴仍默认主旋律，可显式选单音伴奏。
稀疏单音模式继续保留；已有草稿载入不会自动重写，主动切换推荐模式或重新编配后才应用新默认。

本轮三曲验证与限制见 [安全边界报告](TRANSCRIPTION_SAFE_BOUNDARY_REVIEW_2026-10-14.md)，
前次固定边界数据保留在 [精修及和弦报告](TRANSCRIPTION_REFINEMENT_REVIEW_2026-10-14.md)。
本地试听页地址为 `http://127.0.0.1:8769/refinement-safe-boundaries/`，原 `/refinement/` 入口会跳转至此。

顶部选段为原曲时间；结果试听与卷帘从选段 00:00 开始。拖动试听保留已有持续音。
显示网格与节奏整理分开设置。轻度对齐仅在拍点可靠时微调，不代表小节第一拍已准确识别。
模型权重为 CC BY-NC 4.0，限非商业使用；代码为 MIT，相关声明见 THIRD_PARTY_NOTICES.md。

## 混合后端（默认）

新任务默认选择「Demucs＋GAME＋MuScriptor · 默认」，背景使用 MuScriptor small，medium 可选。
打开旧草稿仍还原原后端和保存的曲谱。旧 Hybrid 3.0.0 / MuScriptor 2.0.0 组件继续支持；
Windows 使用全阶段 CUDA 需升级至 Hybrid 3.1.0 的 cu128 包。
GAME 使用作者 GitHub 固定发布包（约 185 MB 下载／200 MB 权重），许可为 CC BY-NC-SA 4.0；
MuScriptor 继续从 ModelScope 下载。两者均校验固定 SHA256，支持本地目录导入，推理阶段不联网下载。

混合任务先解码为 44.1 kHz 浮点选段并进行一次四轨分离，然后顺序运行共享节拍分析、GAME 人声识别、
MuScriptor 背景识别。每阶段独立子进程，完成后释放模型；主程序不加载推理模型。
三个模型阶段都遵循用户所选自动加速／CPU。自动模式在加载模型前检查 CUDA 可用性并执行一次
小矩阵运算，显卡架构或驱动不兼容时记录原因并使用 CPU；显存不足等推理错误明确失败，
用户可选择 small、较短片段或 CPU 重试，不提交不完整结果，不更换模型或裁断音符。
默认背景为 other＋bass＋drums，
开发命令可用 `--background without_drums` 对照。鼓事件不会进入光遇音高谱。

实际输入来源与模型乐器标签分开保存。背景 voice 标签不能当作主唱。
可靠人声乐句优先，短换气不由器乐填满；较长无人声区间允许连续器乐主题接管。
不确定位置保留休止。每个选用、抑制、串音省略和同音边界复核决定都有事件 ID，
旋律决策窗口不会直接裁掉音符尾部。声学同音检查仅提示复核，不无条件合并原生事件。

GAME 长段保留重叠上下文并在模型识别出的休止内推进；边界分歧保留记录，无法找到安全推进点时明确失败。
MuScriptor 保持原生顺序窗口和严格 EOS 检查。分轨背景为选段内音频，输入起点固定为零，
原曲位置始终是「选段起点＋事件时间」。背景末尾只有同时低于绝对／相对门限的数值残留才从模型输入中省略：
RMS 至多 −90 dBFS、峰值至多 −70 dBFS，且分别低于全段峰值 70／50 dB；保留 200 ms 余量，
省略部分须至少 500 ms。范围与门限写入元数据，原始浮点背景保持完整，不因 EOS 失败自动切片、填休止或换模型。

可试听原曲、分离人声、混合背景和光遇谱；关闭「伴奏」声部即可只听主旋律。
原始事件 schema 2 保留 GAME 小数音高；光遇映射和原始 MIDI 的整数音高使用明确取整，首版不输出 pitch bend。
原始 MIDI 保留模型候选，不等同于最终光遇编配。

混合缓存位于 `transcription/hybrid-cache/`，分离、拍点、人声、背景分别校验。
模型与参数、运行组件、输入样本身份均参与识别缓存键；音轨样本哈希忽略 WAV 的时间戳元数据，
同时另存完整文件 SHA256。仅更换背景组合会复用有效人声结果；调整音色、移调或编配不运行模型。
设置中的音频缓存清理会移除派生分轨，保留原始事件、模型、草稿源音频和人工编辑。
混合分轨另有 1 GiB LRU 预算，完成任务后清理较早缓存，当前任务分轨受保护；原始分支事件不随分轨淘汰。
独立组件与原混音组件并存，选择后端仅影响下一次识别，旧草稿保持保存时的结果。

## 缓存和草稿

- 歌曲下载缓存 `transcription/downloads/`：按下载完成时间保留 7 天，命中不续期；启动、每小时、读取时清理。
  应用关闭时不执行清理，下次启动补清理。设置中可手动清理。
- 模型权重 `transcription/models/`：长期保留，单独手动删除；不受歌曲缓存清理影响。
  临时下载保留在 `.partial` 目录，完整校验后才启用。删除模型也会移除该型号未完成下载。
- 原始识别缓存 `transcription/results/`：按源音频 SHA256、范围、模型权重、运行库修订、引擎版本、设备选项和解码配置隔离。
  MuScriptor 还包含运行组件文件身份及可选冻结节拍请求。
  编配参数不进入模型缓存键。缓存损坏会重新识别，不作为成功输出使用。
- 草稿 `transcription/drafts/<id>/`：保存 `events.json`、参数、当前曲谱及模型来源；源音频 `source.audio` / MIDI `source.mid` 长期保留。
  删除模型后已有草稿仍可编辑、重新编配、导出和试听；重新运行模型需要安装对应权重。
  `created` 在首次生成时固定，`updated` 只记录最近保存；接受候选等另建版本有自己的生成时间。
  旧草稿缺少 `created` 时优先采用目录创建时间（不晚于保存时间），不可用时采用迁移前的 `updated`，
  一次保存为推定时间并在提示中标注。无法恢复未曾记录且已丢失的精确首次生成时间。
- 原曲选段/分离音频等派生 WAV 使用独立 1 GiB LRU 预算；清理不删除源音频、原始事件和人工编辑。
- 用户导入 MIDI 继续走原有精确时序通道，无需模型；旧 `pitch.npz` 草稿继续用旧编配逻辑打开，绝不自动转换覆盖。

## 运行结构与边界

FFmpeg 生成 16 kHz 单声道选段；MuScriptor 以顺序 5 秒内部窗口解码，使用 batch=1、
prelude_forcing=true、greedy、beam=1、固定随机种子。跨窗口未结束音符由模型事件追踪延续。
不自行把音频切成独立 5 秒 MIDI 再拼接，不合并所有相邻同键事件。
严格 EOS 检查失败时整个任务报错，旧草稿保留，避免把截断的输出当作完整结果。
零长度或超范围事件被忽略并记录。节拍使用本地信号分析，不调用 beat_this、MuseScore 或额外模型下载。

旋律选择按音符来源、覆盖、复音程度、音高连续性及上下文筛选。模型的 voice 标签不是主唱保证，
有时会把人声识别成管乐；来源分歧时保留休止。来源切换不会截断已建立的长音。
伴奏仅从已有模型事件挑选，再施加光遇音域、同键冲突、音尾、密度和最短时长约束。
谱面显示的模型置信度为 null，不伪造准确率。更换模型不能保证消除所有延音误切或漏音。

每个任务独立工作进程，任务结束释放模型；取消结束进程树。模型使用期间持有跨进程锁，
阻止下载/删除同一模型。所有推理显式加载本地文件并设置 HF_HUB_OFFLINE，不回退到 Hugging Face。
权重固定为 ModelScope 镜像版本；镜像与官方同尺寸，不宣称已证明与官方逐字节一致。

## 开发、复现与打包

```sh
PYTHONPATH=src uv run python scripts/prepare_muscriptor.py
PYTHONPATH=src uv run python scripts/prepare_muscriptor.py --skip-install --download both --data '/path/to/AutoSkyPlay-data'
uv run autoskyplay --transcribe
PYTHONPATH=src uv run python scripts/prepare_muscriptor.py --skip-install --package
```

运行库固定为 `7f213afecf23bd6a1b8672aa223690ee9807cefb`，只安装转谱核心所需依赖，
不安装上游 Web UI 或 beat_this。独立环境位于 `build/muscriptor-venv`。
`engine/muscriptor-requirements.lock` 以 Apple Silicon Python 3.12 实测版本为基础，
Windows 添加 PyTorch CUDA 变体及平台依赖解析检查；依赖解析不等于真机推理验证。
运行组件按平台和文件哈希校验，保存在 `transcription/muscriptor/components/`；与旧组件分开。
清理运行组件保留 MuScriptor 权重、曲谱与源文件。源码开发环境由脚本管理，不受该按钮影响。

```sh
QT_QPA_PLATFORM=offscreen uv run pytest -q
uv run ruff check src tests scripts
PYTHONPATH=src uv run python scripts/evaluate_muscriptor.py
```

对照脚本要求 `artifacts/muscriptor-2026-10-14/` 中三首歌两档模型的原始结果，以及已冻结的旧版基线。
它生成三版本曲谱、原始 MIDI、17 个窗口的钢琴/小提琴试听和结构指标，不将结构指标称为准确率。
性能与真实歌曲对比见 [MuScriptor 迁移验证报告](TRANSCRIPTION_MUSCRIPTOR_REVIEW_2026-10-14.md)。
旧引擎和历史构建说明保留在 [旧版文档](TRANSCRIPTION_LEGACY.md)。

混合后端复现入口：

```sh
PYTHONPATH=src uv run python scripts/prepare_hybrid.py --package
PYTHONPATH=src uv run python scripts/run_hybrid.py /path/to/song.wav --data /path/to/app-data --cache /path/to/cache --output /path/to/result --start 0 --length 60
PYTHONPATH=src uv run python scripts/evaluate_hybrid.py --recognize-legacy
```

运行锁文件为 `engine/hybrid-requirements.lock`，对应本次 macOS arm64 / Python 3.12 实测环境。
GAME 与 MuScriptor 的代码修订、模型哈希和第三方声明分别保存在模型管理模块及组件清单中。
真实歌曲对照与启用取舍见 [混合后端验证报告](TRANSCRIPTION_HYBRID_REVIEW_2026-10-14.md)。

### Windows CUDA 组件

在 Windows x64 / Python 3.12 上执行（命令提示符或 PowerShell）：

```text
uv run python scripts/prepare_hybrid.py --package
uv run python scripts/prepare_muscriptor.py --package
```

Windows 的 `--torch-runtime auto` 固定使用 PyTorch 2.8.0 + CUDA 12.8，混合组件的 TorchAudio
也固定为 2.8.0 + cu128，来自 [PyTorch 官方发布索引](https://pytorch.org/get-started/previous-versions/#v280)。
这里按目标平台选择运行库，不依据构建机器有没有 GPU；CPU CI 主机也必须打包 CUDA 运行库。
构建会校验 torch.version.cuda、TorchAudio 变体、冻结入口及 CUDA DLL，防止发布成仅 CPU 的包。
`runtime.json` 记录具体运行环境；更换组件会改变识别缓存身份。三个阶段顺序运行，不同时常驻显存。
阶段日志记录实际设备、CPU 回退原因以及 CUDA 分配／保留显存峰值。

用户需要兼容的 NVIDIA GPU 和驱动，程序不安装或更新显卡驱动。CUDA 不可用（包括其他品牌显卡）时使用 CPU；
也可手动选择 CPU。需要较小的纯 CPU 组件时，构建命令追加 `--torch-runtime cpu`，产物以 `-cpu.zip` 区分。
更新运行环境后再使用 `--skip-install --package`；该参数仍会校验目标 CUDA/CPU 变体，不接受环境不匹配。
GitHub 的 Build desktop app 工作流同时生成主程序、Hybrid 和 MuScriptor 组件；Windows 产物默认以 `-cu128.zip` 结尾。

本次实现与验证范围见 [默认后端、CUDA 和草稿排序验证](TRANSCRIPTION_CUDA_REVIEW_2026-10-15.md)。
