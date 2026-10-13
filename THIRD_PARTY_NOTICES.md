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

独立 CPU 组件使用 [Demucs](https://github.com/adefossez/demucs) 4.1.0（MIT）和
[torchcrepe](https://github.com/maxrmorrison/torchcrepe) 0.0.24（MIT），以及
[HTDemucs 模型](https://huggingface.co/adefossez/HTDemucs)，固定修订与文件哈希见
`engine/models.json`。torchcrepe 随包提供由原 CREPE 权重转换的 full/tiny 模型。
相关来源、模型说明与已安装依赖的许可证随组件包保存。

解码使用 imageio-ffmpeg 0.6.0 随包 FFmpeg 二进制，其源代码和构建说明见
[imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) 与
[FFmpeg 构建项目](https://github.com/BtbN/FFmpeg-Builds)。FFmpeg 许可证以对应发行构建为准；
组件的 `licenses/` 目录收录各依赖分发中的 LICENSE/COPYING/NOTICE 与 FFmpeg 的 `-L` 输出，
主程序仍按 GPL-3.0 分发。
