"""Cancellable runtime inspection, without loading torch in the Qt process."""
from __future__ import annotations

import json

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

TITLES = dict(separate='分轨', separation='分轨', game='人声', background='背景', muscriptor='原混音')


class DeviceStatus(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.summary = QLabel('设备尚未检测')
        self.summary.setWordWrap(True)
        self.summary.setMinimumWidth(0)
        self.refresh_button = QPushButton('↻')
        self.refresh_button.setToolTip('重新检测设备')
        self.refresh_button.setAccessibleName('重新检测设备')
        self.refresh_button.setFixedSize(28, 28)
        self.setFixedHeight(44)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.summary, 1)
        layout.addWidget(self.refresh_button)
        self.process = QProcess(self)
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(self.failed)
        self.timeout = QTimer(self)
        self.timeout.setSingleShot(True)
        self.timeout.timeout.connect(self.expired)
        self.jobs, self.reports = [], {}
        self.identity = None
        self.requested = 'auto'
        self.buffer = b''
        self.current = ''
        self.stopped = False
        self.process.readyReadStandardOutput.connect(self.read_output)

    def stop(self):
        self.stopped = True
        self.jobs.clear()
        self.timeout.stop()
        self.process.kill()
        self.process.waitForFinished(1000)

    def refresh(self, engine, requested, force=False):
        identity = (repr(engine), requested)
        if identity == self.identity and not force:
            return
        self.stop()
        self.identity, self.requested = identity, requested
        self.reports = {}
        if engine.version not in ('3.2.0', '2.2.0'):
            self.summary.setText('组件不支持预检，请升级')
            self.summary.setToolTip('识别时仍按原设备策略执行；新版组件提供硬件检测和 CPU 自动重试。')
            return
        commands = engine.config.get('commands', {})
        self.jobs = ([(s, commands[s]) for s in ('separate', 'game', 'background')]
                     if engine.backend == 'hybrid' else [('muscriptor', engine.command)])
        self.stopped = False
        self.next_job()

    def next_job(self):
        if self.stopped or self.process.state() != QProcess.ProcessState.NotRunning:
            return
        if not self.jobs:
            self.show_reports()
            return
        self.current, command = self.jobs.pop(0)
        self.buffer = b''
        self.summary.setText(f'正在检测设备 · {TITLES.get(self.current, self.current)}…')
        env = QProcessEnvironment.systemEnvironment()
        for key in env.keys():
            if (key.startswith(('AUTOSKYPLAY_NETEASE', 'NETEASE_COOKIE', 'MUSIC_U')) or
                    key in ('PYTHONPATH', 'PYTHONHOME', 'LD_PRELOAD', 'DYLD_INSERT_LIBRARIES')):
                env.remove(key)
        env.insert('HF_HUB_OFFLINE', '1')
        env.insert('PYTHONIOENCODING', 'utf-8')
        self.process.setProcessEnvironment(env)
        self.process.start(command[0], [*command[1:], '--probe-devices', '--device', self.requested])
        self.timeout.start(30000)

    def read_output(self):
        self.buffer += bytes(self.process.readAllStandardOutput())
        if len(self.buffer) > 256 * 1024:
            self.stop()
            self.summary.setText('设备检测输出异常；可重新检测')

    def finished(self, code, status):
        self.timeout.stop()
        if self.stopped:
            return
        self.read_output()
        report = None
        if code == 0:
            for line in self.buffer.splitlines():
                try:
                    value = json.loads(line)
                    if (value.get('stage') == 'devices' and value.get('schema') == 1
                            and isinstance(value.get('cpu'), dict) and 'name' in value['cpu']
                            and isinstance(value.get('gpus'), list)
                            and all(isinstance(g, dict) and 'name' in g and 'device' in g for g in value['gpus'])
                            and isinstance(value.get('stages'), dict)):
                        report = value
                except (ValueError, AttributeError):
                    pass
        self.reports[self.current] = report or {'error': '检测失败，识别时将重新检查'}
        QTimer.singleShot(0, self.next_job)

    def expired(self):
        self.stop()
        self.summary.setText('设备检测超时，可重新检测')
        self.summary.setToolTip('识别时仍会检查实际设备。')

    def failed(self, error):
        if error == QProcess.ProcessError.FailedToStart and not self.stopped:
            self.stop()
            self.summary.setText('组件无法启动，请重新安装')

    def show_reports(self):
        rows, details, names = [], [], []
        for stage, report in self.reports.items():
            if 'error' in report:
                rows.append(f'{TITLES[stage]}：检测失败')
                details.append(report['error'])
                continue
            actual = report.get('stages', {}).get(stage, 'cpu')
            cpu = report['cpu']['name']
            gpu = next((g['name'] for g in report['gpus'] if g['device'] == actual), '')
            rows.append(f'{TITLES[stage]} {actual.upper()}')
            names.append(gpu or cpu)
            details.append(f'{TITLES[stage]}：{gpu or cpu} · {actual.upper()}')
            details.append(f"CPU：{cpu} · {report['cpu'].get('logical_cores', '?')} 个逻辑核心")
            for g in report['gpus']:
                details.append(f"{g['name']}：{'运算探测通过' if g.get('usable') else g.get('reason', '不可用')}")
        name = next(iter(dict.fromkeys(names)), '')
        choices = {s: r.get('stages', {}).get(s) for s, r in self.reports.items()}
        if len(choices) == 3 and len(set(choices.values())) == 1 and all(choices.values()):
            stages = '全阶段 ' + next(iter(choices.values())).upper()
        elif len(choices) == 3 and choices.get('separate') == choices.get('game') and all(choices.values()):
            stages = f"分轨/人声 {choices['game'].upper()} · 背景 {choices['background'].upper()}"
        else:
            stages = ' · '.join(rows)
        name = self.summary.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, max(100, self.width()-34))
        self.summary.setText(name + '\n预计：' + stages)
        reasons = []
        for report in self.reports.values():
            if self.requested != 'cpu' and report.get('cuda_unavailable'):
                reasons.append(report['cuda_unavailable'])
            if report.get('mps_unavailable'):
                reasons.append(report['mps_unavailable'])
            elif report.get('limitations') and 'separate' in self.reports:
                reasons.append(report['limitations']['mps'])
        self.summary.setToolTip('\n'.join(dict.fromkeys(details)) + '\n\n' + '\n'.join(dict.fromkeys(reasons)))

    def execution(self, event):
        self.stop()
        actual = event.get('actual_device', '未知')
        stage = TITLES.get(event.get('engine_stage', ''), event.get('engine_stage', ''))
        prefix = '复用缓存' if event.get('cached') else '实际使用'
        fallback = event.get('fallback')
        selection = event.get('device_selection', {})
        reasons = []
        if actual == 'cpu' and event.get('requested') == 'auto':
            reasons = [selection.get('cuda_unavailable', ''), selection.get('mps_unavailable', '')]
            if event.get('engine_stage') in ('game', 'separate', 'separation'):
                reasons.extend(selection.get('limitations', {}).values())
        self.summary.setText(f'{stage} · {prefix}：{actual.upper()}' +
                             ('\nGPU 失败，CPU 重试一次' if fallback and not event.get('cached')
                              else '\n缓存由 CPU 回退生成' if fallback else ''))
        self.summary.setToolTip(
            f'{stage}：{"缓存生成时使用" if event.get("cached") else "当前使用"} {actual.upper()}' +
            '\n' + selection.get('gpu', selection.get('cpu', {}).get('name', '')) +
            '\n'.join(['', *[r for r in reasons if r]]) +
            ('\nGPU 回退原因：' + fallback.get('reason', '') + '\n使用相同模型与完整选段重试，耗时可能增加。'
             if fallback else ''))
