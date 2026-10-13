# AutoSkyPlay

使用 Python / PySide6 重写 SMAP，面向 Windows 游戏演奏，支持 macOS/Linux 试听、制谱与跟弹。

原作者仓库：[Sky-MusaAutoPlay-SMAP-](https://github.com/lingyunalingyun/Sky-MusaAutoPlay-SMAP-)（作者：lingyunalingyun）。

已实现曲库、播放队列、37 种音色、JSON/TXT/MIDI 导入、卷帘编辑器、跟弹练习、
云端曲库、主题与设置。Windows 游戏输入已实现，仍需 Windows 实机验收。
详细功能和验证边界见 [重写记录](docs/MIGRATION.md)。

```sh
uv sync --extra dev
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

云端使用缪斯树屋服务。程序默认 HTTPS；在登录界面可选用系统凭据库记住令牌，
不保存密码。上传曲谱、日志和下载安装更新需在对应界面手动触发。

## 数据目录

- Windows：`%LOCALAPPDATA%\AutoSkyPlay`
- macOS：`~/Library/Application Support/AutoSkyPlay`
- Linux：`~/.local/share/AutoSkyPlay`

可用 `autoskyplay --data-dir PATH` 指定目录。「软件信息 → 打开文件位置」可打开当前目录。
损坏的设置会备份为 `state.broken*.json`，随后恢复默认值。

## 打包与开发

```sh
uv sync --frozen --extra dev
uv run pytest -q
uv run ruff check src tests scripts
uv run python scripts/build.py
```

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
