from __future__ import annotations

import json

from PySide6.QtGui import QColor, QPalette

from ..audio import ASSETS

LANGUAGES = {"zh_CN": "简体中文", "zh_TW": "繁體中文", "en": "English", "ja": "日本語"}
TRANSLATIONS = json.loads((ASSETS / "translations.json").read_text(encoding="utf-8"))
EXTRA = {
    "app.name": ["AutoSkyPlay", "AutoSkyPlay", "AutoSkyPlay", "AutoSkyPlay"],
    "library": ["本地曲库", "本地曲庫", "Local library", "ローカル曲庫"],
    "cloud": ["云端曲库", "雲端曲庫", "Cloud library", "オンライン曲庫"],
    "favorites": ["我的收藏夹", "我的收藏夾", "Collections", "コレクション"],
    "search": [
        "搜索曲名、作者、创谱者…",
        "搜尋曲名、作者、創譜者…",
        "Search songs, artists, transcribers…",
        "曲名・作曲者・採譜者を検索…",
    ],
    "import": ["导入曲谱", "匯入曲譜", "Import scores", "曲譜をインポート"],
    "create": ["创建曲谱", "建立曲譜", "New score", "曲譜を作成"],
    "practice": ["跟弹练习", "跟彈練習", "Practice", "練習"],
    "queue": ["播放队列", "播放佇列", "Play queue", "再生キュー"],
    "title": ["曲名", "曲名", "Title", "曲名"],
    "artist": ["作者", "作者", "Artist", "作曲者"],
    "transcriber": ["创谱者", "創譜者", "Transcriber", "採譜者"],
    "duration": ["时长", "時長", "Duration", "時間"],
    "settings": ["设置", "設定", "Settings", "設定"],
    "save": ["保存", "儲存", "Save", "保存"],
    "saveas": ["另存为", "另存新檔", "Save as", "名前を付けて保存"],
    "cancel": ["取消", "取消", "Cancel", "キャンセル"],
    "edit": ["编辑曲谱", "編輯曲譜", "Edit score", "曲譜を編集"],
    "addqueue": ["加入队列", "加入佇列", "Add to queue", "キューに追加"],
    "remove": ["从曲库移除", "從曲庫移除", "Remove from library", "曲庫から削除"],
    "location": ["打开文件位置", "開啟檔案位置", "Open file location", "ファイルの場所を開く"],
    "info": ["歌曲信息", "歌曲資訊", "Song information", "曲の情報"],
    "upload": ["上传到云端", "上傳至雲端", "Upload to cloud", "クラウドにアップロード"],
    "download": ["下载选中曲谱", "下載選取曲譜", "Download selected", "選択した曲譜をダウンロード"],
    "more": ["加载更多", "載入更多", "Load more", "さらに読み込む"],
    "empty": [
        "还没有曲谱。导入 JSON / TXT / MIDI，或创建你的第一首曲子。",
        "尚無曲譜。匯入 JSON / TXT / MIDI，或建立你的第一首曲子。",
        "Import JSON / TXT / MIDI, or create your first score.",
        "JSON / TXT / MIDIをインポートするか、新しい曲譜を作成してください。",
    ],
    "preview": ["试听", "試聽", "Preview", "試聴"],
    "perform": ["游戏演奏", "遊戲演奏", "Play in game", "ゲーム演奏"],
    "ready": ["准备就绪", "準備就緒", "Ready", "準備完了"],
    "stop": ["停止", "停止", "Stop", "停止"],
    "play": ["播放", "播放", "Play", "再生"],
    "pause": ["暂停", "暫停", "Pause", "一時停止"],
    "resume": ["继续", "繼續", "Resume", "再開"],
    "previous": ["上一首", "上一首", "Previous", "前の曲"],
    "next": ["下一首", "下一首", "Next", "次の曲"],
    "demonstrate": ["示范播放", "示範播放", "Demonstration", "お手本再生"],
    "beatmode": ["自动打点", "自動打點", "Beat mode", "ビートモード"],
    "gamepractice": [
        "游戏练习（置顶 / 全局监听）",
        "遊戲練習（置頂 / 全域監聽）",
        "Game practice (always on top)",
        "ゲーム練習（最前面）",
    ],
    "back": ["返回 · Esc", "返回 · Esc", "Back · Esc", "戻る · Esc"],
    "reset": ["重新开始", "重新開始", "Restart", "最初から"],
    "completed": ["练习完成", "練習完成", "Practice complete", "練習完了"],
    "newcollection": ["新建收藏夹", "新增收藏夾", "New collection", "コレクションを作成"],
    "rename": ["重命名", "重新命名", "Rename", "名前を変更"],
    "delete": ["删除", "刪除", "Delete", "削除"],
    "refresh": ["刷新", "重新整理", "Refresh", "更新"],
    "login": ["登录", "登入", "Sign in", "ログイン"],
    "logout": ["退出登录", "登出", "Sign out", "ログアウト"],
    "speed": ["倍速", "倍速", "Speed", "速度"],
    "volume": ["音量", "音量", "Volume", "音量"],
    "sequence": ["顺序播放", "順序播放", "Play in order", "順番に再生"],
    "cyclemode": ["点击切换播放方式", "點擊切換播放方式", "Click to change play mode", "クリックで切替"],
    "showqueue": ["展开播放列表", "展開播放清單", "Show play queue", "再生キューを表示"],
    "hidequeue": ["隐藏播放列表 · Esc", "隱藏播放清單 · Esc", "Hide play queue · Esc", "再生キューを閉じる · Esc"],
    "queueempty": [
        "播放列表为空。双击曲库中的歌曲即可加入并播放。",
        "播放清單為空。按兩下曲庫中的歌曲即可加入並播放。",
        "Your queue is empty. Double-click a library song to add and play it.",
        "キューは空です。曲庫の曲をダブルクリックして追加・再生できます。",
    ],
    "favorite": ["收藏歌曲", "收藏歌曲", "Favorite song", "お気に入りに追加"],
    "unfavorite": ["取消收藏", "取消收藏", "Remove favorite", "お気に入りから削除"],
    "perform.windows": [
        "游戏演奏仅支持 Windows", "遊戲演奏僅支援 Windows",
        "Game performance is available on Windows", "ゲーム演奏はWindowsで利用できます",
    ],
    "listloop": ["列表循环", "清單循環", "Repeat queue", "キューをループ"],
    "singleloop": ["单曲循环", "單曲循環", "Repeat one", "1曲をループ"],
    "shuffle": ["随机播放", "隨機播放", "Shuffle", "シャッフル"],
    "clearqueue": ["清空队列", "清空佇列", "Clear queue", "キューをクリア"],
    "cover": ["设置封面", "設定封面", "Set cover", "カバーを設定"],
    "undo": ["撤销", "復原", "Undo", "元に戻す"],
    "redo": ["重做", "重做", "Redo", "やり直す"],
    "keys.reset": ["恢复默认键位", "恢復預設鍵位", "Reset key bindings", "既定のキーに戻す"],
    "login.user": ["用户名", "使用者名稱", "Username", "ユーザー名"],
    "login.pass": ["密码", "密碼", "Password", "パスワード"],
    "midi.auto": ["自动推荐移调", "自動建議移調", "Suggest transposition", "移調を自動検出"],
    "set.fontScale": [
        "字体大小（界面随字体缩放）",
        "字型大小（介面隨字型縮放）",
        "Font size (scales the UI)",
        "フォントサイズ（UIも拡大）",
    ],
    "set.uploadLog": ["上传日志", "上傳日誌", "Upload logs", "ログをアップロード"],
    "grid": ["网格", "網格", "Grid", "グリッド"],
    "beats": ["总拍数", "總拍數", "Beats", "拍数"],
    "notelength": ["音符拍长", "音符拍長", "Note length", "音符の長さ"],
    "apply": ["应用到选中音符", "套用至選取音符", "Apply to selection", "選択に適用"],
    "remember": ["记住登录", "記住登入", "Remember login", "ログイン状態を保存"],
    "rememberhint": [
        "令牌保存到系统凭据库，密码不会保存。",
        "權杖儲存至系統憑證庫，不儲存密碼。",
        "Tokens are stored in the system keyring. Passwords are never saved.",
        "トークンはシステムの資格情報に保存されます。パスワードは保存しません。",
    ],
}
TRANSLATIONS.update(EXTRA)
current_language = "zh_CN"


def tr(key: str) -> str:
    values = TRANSLATIONS.get(key)
    return values[list(LANGUAGES).index(current_language)] if values else key


THEMES = {
    "dark": {
        "bg": "#12161c",
        "panel": "#1b212a",
        "raised": "#242d38",
        "text": "#edf1f7",
        "muted": "#8e9cad",
        "border": "#303c4b",
        "accent": "#8bd6c2",
        "ink": "#142620",
    },
    "light": {
        "bg": "#f1f4f5",
        "panel": "#ffffff",
        "raised": "#e9eff1",
        "text": "#202f3b",
        "muted": "#637581",
        "border": "#d5e0e3",
        "accent": "#257d71",
        "ink": "#ffffff",
    },
    "sunset": {
        "bg": "#261c2c",
        "panel": "#332739",
        "raised": "#463344",
        "text": "#fff0e5",
        "muted": "#bd9faf",
        "border": "#634659",
        "accent": "#f5ba93",
        "ink": "#38242c",
    },
}
colors = THEMES["dark"]


def apply_theme(app, theme: str, font_size: int = 13):
    global colors
    colors = THEMES.get(theme, THEMES["dark"])
    c = colors
    palette = QPalette()
    for role, color in {
        QPalette.ColorRole.Window: c["bg"],
        QPalette.ColorRole.WindowText: c["text"],
        QPalette.ColorRole.Base: c["panel"],
        QPalette.ColorRole.Text: c["text"],
        QPalette.ColorRole.Button: c["raised"],
        QPalette.ColorRole.ButtonText: c["text"],
        QPalette.ColorRole.Highlight: c["accent"],
        QPalette.ColorRole.HighlightedText: c["ink"],
    }.items():
        palette.setColor(role, QColor(color))
    app.setPalette(palette)
    app.setStyleSheet(f"""
        QWidget {{ color: {c["text"]}; font-size: {font_size}px; }}
        QMainWindow, QDialog {{ background: {c["bg"]}; }}
        QFrame#panel {{ background: {c["panel"]}; border: 1px solid {c["border"]}; border-radius: 14px; }}
        QLabel#muted {{ color: {c["muted"]}; }}
        QLabel#heading {{ font-size: 26px; font-weight: 700; }}
        QLabel#brand {{ font-size: 22px; font-weight: 700; }}
        QPushButton {{ background: {c["raised"]}; border: 1px solid {c["border"]}; border-radius: 8px;
                       padding: 8px 12px; }}
        QPushButton:hover {{ border-color: {c["accent"]}; }}
        QPushButton:pressed, QPushButton:checked {{ background: {c["accent"]}; color: {c["ink"]}; }}
        QPushButton#accent {{ background: {c["accent"]}; color: {c["ink"]}; font-weight: 600; }}
        QPushButton#transport {{ padding: 0; }}
        QPushButton#favorite {{ padding: 0; background: transparent; border-color: transparent;
                                color: {c["accent"]}; font-size: 20px; }}
        QPushButton#favorite:hover {{ background: {c["raised"]}; border-color: {c["accent"]}; }}
        QFrame#modeSwitch {{ background: {c["raised"]}; border: 1px solid {c["border"]}; border-radius: 8px; }}
        QPushButton#modeOption {{ background: transparent; border: 1px solid transparent;
                                  color: {c["muted"]}; padding: 6px 10px; }}
        QPushButton#modeOption:checked {{ color: {c["accent"]}; font-weight: 700; }}
        QPushButton#modeOption:hover {{ color: {c["text"]}; }}
        QPushButton#modeOption:checked:hover {{ color: {c["accent"]}; }}
        QPushButton#transport:focus, QPushButton#favorite:focus, QPushButton#modeOption:focus {{
            border-color: {c["accent"]}; }}
        QPushButton#modeOption:disabled {{ color: {c["muted"]}; }}
        QPushButton:disabled {{ color: {c["muted"]}; }}
        QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit {{ background: {c["panel"]};
            border: 1px solid {c["border"]}; border-radius: 7px; padding: 7px; selection-background-color: {c["accent"]}; }}
        QComboBox QAbstractItemView {{ background: {c["panel"]}; selection-background-color: {c["accent"]}; }}
        QTableWidget, QListWidget {{ background: {c["panel"]}; border: none; outline: 0; border-radius: 8px; }}
        QTableWidget::item, QListWidget::item {{ padding: 9px; border-bottom: 1px solid {c["border"]}; }}
        QTableWidget::item:selected, QListWidget::item:selected {{ background: {c["raised"]}; color: {c["accent"]}; }}
        QHeaderView::section {{ background: {c["panel"]}; color: {c["muted"]}; border: none; padding: 12px; }}
        QScrollArea {{ border: none; background: transparent; }}
        QScrollBar:vertical {{ background: transparent; width: 8px; }}
        QScrollBar::handle:vertical {{ background: {c["border"]}; border-radius: 4px; min-height: 25px; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ height: 0px; width: 0px; }}
        QSlider::groove:horizontal {{ background: {c["border"]}; height: 5px; border-radius: 2px; }}
        QSlider::sub-page:horizontal {{ background: {c["accent"]}; border-radius: 2px; }}
        QSlider::handle:horizontal {{ background: {c["accent"]}; width: 13px; margin: -5px 0; border-radius: 6px; }}
        QSlider::groove:vertical {{ background: {c["border"]}; width: 5px; border-radius: 2px; }}
        QSlider::add-page:vertical {{ background: {c["accent"]}; border-radius: 2px; }}
        QSlider::handle:vertical {{ background: {c["accent"]}; height: 13px; margin: 0 -5px; border-radius: 6px; }}
        QMenu {{ background: {c["panel"]}; border: 1px solid {c["border"]}; padding: 6px; }}
        QMenu::item {{ padding: 9px 20px; }}
        QMenu::item:selected {{ background: {c["raised"]}; }}
        QToolTip {{ background: {c["raised"]}; color: {c["text"]}; padding: 5px; }}
        QCheckBox {{ spacing: 8px; }}
    """)
