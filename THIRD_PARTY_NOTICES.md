# Third-party notices

AutoSkyPlay is a Python rewrite based on the features and public source of
[Sky-MusaAutoPlay-SMAP-](https://github.com/lingyunalingyun/Sky-MusaAutoPlay-SMAP-).
Reference revision: `0416e82fa95c472de2de7eaf96ddd05445db73fc`.

The upstream project is licensed under GNU GPL v3. This project preserves that
license; see LICENSE. Application logic and desktop UI were rewritten in Python.
No C# or Java executable/runtime is required.

The following data assets are retained from upstream:

- `src/autoskyplay/assets/instruments/`: 555 WAV files in 37 instrument banks.
  Upstream attributes samples to Sky Studio and sounds extracted from Sky.
  Rights to game sounds remain with their respective owners; inclusion in the
  upstream repository does not establish a separate grant from those owners.
- `src/autoskyplay/assets/logo.png` and `app.ico`: upstream application branding.
- `src/autoskyplay/assets/translations.json`: translation strings extracted from
  upstream `Lang.cs`.

The third-party song collection is not bundled. Importing a score does not
transfer its copyright. The bundled `exercise.json` is an original exercise
created for AutoSkyPlay.

Python dependencies retain their own licenses. PySide6/Qt are used through their
open-source distribution; their notices accompany PyInstaller bundles. Review
asset rights and dependency license obligations before public distribution.

The embedded NetEase metadata/login service uses
[@neteasecloudmusicapienhanced/api](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced)
version 4.41.1 under the MIT license. Its production dependency versions and
integrity hashes are recorded in `src/autoskyplay/assets/netease/package-lock.json`.
The service adapter is application code; the upstream modules are installed
unchanged. The package and dependency license files accompany the bundled
`assets/netease/node_modules/` tree.

Node.js 22.22.0 is redistributed with platform-specific application builds.
Its MIT license and bundled third-party notices are included in
`assets/netease/runtime/LICENSE`. The runtime is downloaded from nodejs.org and
verified against the release's official SHA-256 checksum manifest.

# 本地扒谱可选组件

MuScriptor 2.0.0 组件使用 [MuScriptor](https://github.com/muscriptor/muscriptor) 转谱核心，
固定代码修订 `7f213afecf23bd6a1b8672aa223690ee9807cefb`（MIT，Kyutai x Mirelo）。
原始 MIDI 的乐器组映射依据该修订中的 `muscriptor/tokenizer/mt3.py`。
small / medium 权重遵循 **CC BY-NC 4.0，限非商业用途**，署名 MuScriptor / Kyutai x Mirelo；
许可证：https://creativecommons.org/licenses/by-nc/4.0/ 。
官方来源为 https://huggingface.co/MuScriptor/muscriptor-small 和
https://huggingface.co/MuScriptor/muscriptor-medium 。
本项目从 ModelScope 同名社区镜像下载，固定修订和 SHA256 见
`src/autoskyplay/transcription/models.py`，不修改权重，不宣称镜像为作者官方发布渠道。
运行组件与权重分开管理，模型不会随 GPL 应用代码改变许可；应用的 GPL 许可不赋予模型商业使用权。
MuScriptor 的 MIT 许可证和所用依赖许可证随独立运行组件提供。

混合 3.0.0 可选组件增加 [GAME](https://github.com/openvpi/GAME)，
固定代码修订 `f4239345f0fed21890d362c74a35fd857724aa82`（openvpi，MIT）。
GAME 1.0 medium 权重从作者的
[v1.0.0 发布页](https://github.com/openvpi/GAME/releases/tag/v1.0.0) 下载，
遵循 **CC BY-NC-SA 4.0**；许可证：https://creativecommons.org/licenses/by-nc-sa/4.0/ 。
固定发布包及内部文件 SHA256 见 `src/autoskyplay/transcription/game_models.py`。
不修改权重。GAME 与 MuScriptor 的模型许可分别适用，不因同一组件分发而变更。
本项目直接调用 GAME 的推理模块，不包含 Qwen、歌词 ASR 或强制对齐模型。

独立 CPU 组件使用 [Demucs](https://github.com/adefossez/demucs) 4.1.0（MIT）和
[torchcrepe](https://github.com/maxrmorrison/torchcrepe) 0.0.24（MIT），以及
[HTDemucs 模型](https://huggingface.co/adefossez/HTDemucs)，固定修订与文件哈希见
`engine/models.json`。torchcrepe 随包提供由原 CREPE 权重转换的 full/tiny 模型。
相关来源、模型说明与已安装依赖的许可证随组件包保存。

Windows Hybrid / MuScriptor 组件使用 PyTorch 官方 2.8.0 + cu128 发行包，
混合组件搭配同版本 TorchAudio。随 wheel 提供的 NVIDIA CUDA、cuBLAS、cuDNN 等运行库
保留各自许可证；构建脚本收集 wheel 内的 LICENSE/COPYING/NOTICE，随组件 `licenses/` 分发。
来源与构建选择见 [PyTorch 官方版本说明](https://pytorch.org/get-started/previous-versions/#v280)。

解码使用 imageio-ffmpeg 0.6.0 随包 FFmpeg 二进制，其源代码和构建说明见
[imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) 与
[FFmpeg 构建项目](https://github.com/BtbN/FFmpeg-Builds)。FFmpeg 许可证以对应发行构建为准；
组件的 `licenses/` 目录收录各依赖分发中的 LICENSE/COPYING/NOTICE 与 FFmpeg 的 `-L` 输出，
主程序仍按 GPL-3.0 分发。
