# AutoSkyPlay

使用 Python / PySide6 重写 SMAP，面向 Windows 游戏演奏，支持 macOS/Linux 试听、制谱与跟弹。

原作者仓库：[Sky-MusaAutoPlay-SMAP-](https://github.com/lingyunalingyun/Sky-MusaAutoPlay-SMAP-)（作者：lingyunalingyun）。

已实现曲库、播放队列、37 种音色、JSON/TXT/MIDI 导入、卷帘编辑器、跟弹练习、
云端曲库、主题与设置。Windows 游戏输入已实现，仍需 Windows 实机验收。
详细功能和验证边界见 [重写记录](docs/MIGRATION.md)。

```sh
uv sync --extra dev
uv run python scripts/prepare_netease.py
uv run autoskyplay
```

也可使用 Python 3.11+：`python -m pip install -e ".[dev]"`，然后运行 `autoskyplay`。

## 使用

1. 导入 `.json` / `.txt` / `.mid` / `.midi`，或直接使用启动时生成的原创练习曲。
2. 双击曲目加入队列并试听。右侧琴键也支持鼠标和物理键盘弹奏。
3. Windows 游戏演奏：在底部选择「游戏演奏」，拿出游戏乐器，开始后在倒计时内切回游戏。
4. 右键曲目可编辑、收藏、上传或移除；移除的文件保存在数据目录的 `trash/`。
5. 点击「跟弹练习」跟随高亮按键，和弦需同时按住；可切换示范、打点或 Windows 游戏练习。
6. 底部曲名右侧可收藏歌曲。上一曲左侧的按钮按「顺序播放 → 列表循环 → 随机播放 → 单曲循环」切换；顺序播放到队列末尾后停止。
7. 点击停止右侧的列表按钮展开或隐藏播放列表，也可按 Esc 收起。列表可拖动排序、双击播放或右键移除，与右侧音色面板独立。
8. 主页「网易云登录」使用网易云音乐 App 扫码，登录后按钮展示昵称。会话自动保存到项目目录的本地凭据文件，重启后验证并恢复，无需重复扫码；双击账号按钮并确认后可退出并清除保存的会话。
9. 编辑曲目时点击「从网易云获取」，粘贴歌曲链接 / ID 可直接填写；按歌曲名搜索时先查看歌手、专辑和封面，再选择正确版本填写。支持 `163cn.tv` 分享短链接。

网易云信息填写只更新歌曲名、作者（歌手）和专辑封面，保留创谱者、音符及其他曲谱信息。
封面压缩为最长边 512 像素的 JPEG 并嵌入曲谱，之后点击编辑器原有的保存按钮。
封面获取失败时可重试，或选择「仅填写文字，保留现有封面」。
「设置 → 网易云服务端口」默认 3000，保存时检查合法范围及占用情况。

主页「AI扒谱」支持网易云搜索与链接 / ID 选歌、真实波形选段，在本机识别人声主旋律，
试听并生成可编辑的光遇曲谱或 MIDI。支持整首与指定片段，试听音频会明确标注。
默认使用 Demucs＋GAME＋MuScriptor，背景识别使用 small，medium 可选。首次导入 Hybrid 3.1.0 运行组件，
再在「设置 → 扒谱模型管理」安装 GAME medium，并从 ModelScope 下载 MuScriptor 模型；支持取消续传和本地导入。
源码运行先执行 `uv run python scripts/prepare_hybrid.py --package`。
Windows 组件默认包含 CUDA 12.8，GPU 可用时三个阶段优先使用 GPU，不可用时使用 CPU；真机 GPU 验证尚待完成。
模型权重长期保留，限非商业使用，分别遵循 GAME / MuScriptor 许可。工作台支持历史草稿自动保存、只读卷帘预览、
独立音色试听和编辑回写；显示网格与节拍吸附分开设置。原混音草稿支持小节／片段精修，对照试听后接受为新版本，原稿保留。
钢琴默认稀疏双音和弦，保留单音伴奏选项；局部精修可自动向外寻找完整音符边界，显示实际范围后试听接受。
[三曲安全边界验证报告](docs/TRANSCRIPTION_SAFE_BOUNDARY_REVIEW_2026-10-14.md)。网易云下载缓存保留 7 天，
可在主页面设置中清理；草稿源文件长期保留，按首次生成时间倒序排列。选择歌曲窗口支持本地音频 / MIDI，右键草稿可重命名或删除。流程与组件构建见 [AI 扒谱说明](docs/TRANSCRIPTION.md)。

服务在首次登录 / 查询时由应用启动，退出应用时停止；端口修改后下一次请求会启动新端口的服务。
接口使用同一个网易云登录会话，登录 Cookie 不写入设置或日志。
主页登录后编辑页直接使用该会话，显示相同账号并隐藏登录按钮，无需再次扫码。
恢复时网络暂时不可用会保留会话，可点击「重试恢复网易云登录」重试；确认会话失效才会要求重新扫码。
本地凭据目录无法写入时会提示无法保存，本次登录仍可使用。
主页的缪斯树屋和网易云账号已登录时，单击只显示点击动画；双击会弹出退出确认框，默认取消，确认后才退出账号。未登录时仍单击登录。

底部倍速左侧可选择音高；最右侧音量按钮在悬浮时显示可拖动的音量条，离开后隐藏。随机倍速在设置中开启或关闭。

| 快捷键 | 功能 |
| --- | --- |
| F1 | 从头播放 / 停止；练习时重弹 |
| F2 | 暂停 / 继续 |
| F3 / F4 | 减速 / 加速 0.1× |
| F5 / F6 | 上一首 / 下一首 |
| ← / → | 前后 2 秒；练习时前后一步 |
| Y U I O P / H J K L ; / N M , . / | 15 个琴键，可在设置中修改 |
| Ctrl+S / Ctrl+Z / Ctrl+Shift+Z | 编辑器保存 / 撤销 / 重做 |

云端使用缪斯树屋服务。程序默认 HTTPS；在登录界面可选择记住令牌并保存到本地文件，
不保存密码。上传曲谱、日志和下载安装更新需在对应界面手动触发。

## 数据目录

- Windows：`%LOCALAPPDATA%\AutoSkyPlay`
- macOS：`~/Library/Application Support/AutoSkyPlay`
- Linux：`~/.local/share/AutoSkyPlay`

可用 `autoskyplay --data-dir PATH` 指定目录。「软件信息 → 打开文件位置」可打开当前目录。
损坏的设置会备份为 `state.broken*.json`，随后恢复默认值。

两处登录凭据使用项目根目录的 `.autoskyplay/credentials/`，不再访问系统凭据库。
项目内的打包应用也使用这个目录；独立发行包没有项目源码时，使用 `.app` 旁边
（Windows/Linux 为可执行文件旁边）的同名目录，与启动时的工作目录无关。
网易云会话按用户数据目录隔离，缪斯树屋令牌按服务地址隔离；退出登录删除对应文件。
凭据为未加密的本地 JSON，只保存令牌、会话及必要账号信息，不保存账号密码，
不进入 `state.json` 或日志。POSIX 平台目录权限为 700、文件为 600，目录已加入 Git 忽略规则。
原钥匙串中的凭据不会读取或自动迁移，切换后需要重新登录一次。

## 打包与开发

```sh
uv sync --frozen --extra dev
uv run pytest -q
uv run ruff check src tests scripts
uv run python scripts/build.py
```

网易云服务使用固定版本的 [NeteaseCloudMusicApiEnhanced](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced)。
源码准备和构建需要 Node.js 22+（含 npm）；`prepare_netease.py` 按锁文件安装生产依赖，
下载目标平台的 Node 22.22.0 并验证官方 SHA-256。应用发行包携带服务、全部生产依赖和运行时，用户无需另装 Node。
服务升级与适配说明见 [网易云集成说明](docs/NETEASE.md)。

Windows 安装包需在 Windows 上安装 Inno Setup 6，运行：

```powershell
uv run python scripts/build.py --installer
```

输出为 `dist/AutoSkyPlay-Setup.exe`。macOS 构建生成 `.app` 和 `.dmg`。
跨平台构建可手动触发 `.github/workflows/build.yml`。更新仓库设置为 Python 重写版的
`owner/repo`，发布标签使用 `v0.1.1` 等版本号，Windows 安装器资源名使用 `AutoSkyPlay-Setup.exe`。
更新仓库默认留空，尚未绑定远端发布仓库。

核心代码按功能划分：`score` 曲谱、`midi` 导入、`playback` 时钟、`input` Windows 输入、
`audio` 混音、`practice` 和弦判定、`storage` 持久化、`cloud` 在线协议、`ui` 桌面界面。

GPL-3.0。原项目和复用资产来源见 [第三方说明](THIRD_PARTY_NOTICES.md)。

扒谱效果升级支持完整主旋律、器乐段落、简洁伴奏和待修正标记，使用 1.1.0 离线组件。详见 [效果与验证说明](docs/TRANSCRIPTION_QUALITY.md)。

P1 → P2 清晰度优化已实现钢琴稀疏单音伴奏、小提琴默认仅主旋律，以及映射待核对提示；三首歌的对照数据、试听方式和当前取舍见 [清晰度报告](docs/TRANSCRIPTION_CLARITY_REVIEW_2026-10-14.md)。

延音误切修复已加入音轨连续性校验，保守保留真实重奏候选、换气与休止，不直接合并同键音符。三首歌的实际变化、保留的问题和新版试听见 [延音修复报告](docs/TRANSCRIPTION_SUSTAIN_REVIEW_2026-10-14.md)。

默认「Demucs＋GAME＋MuScriptor」混合后端分别识别人声和背景，保留原始事件与来源记录，支持分层缓存、取消和独立冻结组件。
原混音 MuScriptor 后端仍可选；安装、数据兼容和效果边界见 [扒谱使用说明](docs/TRANSCRIPTION.md)。
