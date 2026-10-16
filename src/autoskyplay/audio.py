"""Sample mixer with polyphony, sustained voices, pitch shifting and cave reverb.

The mixer has no device dependency, so DSP can be tested without producing sound.
QtAudioOutput is created on the GUI thread; its callback only reads preloaded data.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

SAMPLE_RATE = 44100
ASSETS = Path(__file__).parent / "assets"
INSTRUMENTS = [
    "Piano",
    "Harp",
    "Guitar",
    "Flute",
    "Ukulele",
    "Winter Piano",
    "Xylophone",
    "Electric Guitar",
    "Bassoon",
    "Orff",
    "Kalimba",
    "Ocarina",
    "Cello",
    "Violin",
    "Saxophone",
    "Pipa",
    "Quena",
    "Bugle",
    "Glock",
    "LightGuitar",
    "GoldPiano",
    "Horn",
    "Handpan",
    "GoldHandpan",
    "Dundun",
    "APBell1",
    "APBell2",
    "Harmonica",
    "AP18Ocarina",
    "AP29Piccolo",
    "GoldBugle",
    "APPiano",
    "4thAnnivArp",
    "4thAnnivLead",
    "Contrabass",
    "4thAnnivBass",
    "GoldDundun",
]
SUSTAIN = {
    "Flute",
    "Ocarina",
    "AP18Ocarina",
    "Saxophone",
    "Harmonica",
    "Horn",
    "Quena",
    "Bugle",
    "GoldBugle",
    "AP29Piccolo",
    "Bassoon",
    "Violin",
    "Cello",
    "Contrabass",
}
DEFAULT_PITCH = {
    name: -12 for name in ("Cello", "Horn", "Handpan", "GoldHandpan", "Dundun", "APBell1", "APBell2")
}
DEFAULT_PITCH.update({name: -24 for name in ("Contrabass", "4thAnnivBass", "GoldDundun")})


def resample(data: np.ndarray, ratio: float) -> np.ndarray:
    if ratio == 1:
        return data.astype(np.float32)
    positions = np.arange(max(1, int(len(data) / ratio))) * ratio
    positions = np.minimum(positions, len(data) - 1)
    return np.column_stack(
        [np.interp(positions, np.arange(len(data)), data[:, channel]) for channel in range(2)]
    ).astype(np.float32)


def loop_sample(data: np.ndarray) -> np.ndarray:
    amplitude = np.max(np.abs(data), axis=1)
    peak = float(amplitude.max())
    audible = np.flatnonzero(amplitude > peak * 0.12)
    if len(audible) < 100:
        return data
    end = int(audible[-1]) + 1
    start = min(int(amplitude.argmax()) + SAMPLE_RATE // 20, max(0, end - SAMPLE_RATE // 4))
    section = data[start:end]
    cross = min(len(section) // 3, SAMPLE_RATE // 8)
    if cross < 10:
        return data
    result = section[:-cross].copy()
    fade = np.linspace(0, 1, cross, dtype=np.float32)[:, None]
    result[:cross] = result[:cross] * fade + section[-cross:] * (1 - fade)
    return result


@dataclass
class Voice:
    data: np.ndarray
    key: int
    loop: bool = False
    position: int = 0
    release_frames: int | None = None
    remaining_frames: int | None = None


class Mixer:
    def __init__(self, assets: Path = ASSETS / "instruments"):
        self.assets = assets
        self.instrument = "Piano"
        self.pitch = 0
        self.volume = 0.6
        self.cave = False
        self.voices: list[Voice] = []
        self.cache = {}
        self.lock = threading.RLock()
        self.delay_buffers = [
            np.zeros((int(SAMPLE_RATE * t), 2), np.float32) for t in (0.113, 0.173, 0.239, 0.307)
        ]
        self.delay_positions = [0] * 4
        self.metronome_bpm = 0.0
        self.metro_frame = 0
        t = np.arange(int(SAMPLE_RATE * 0.035)) / SAMPLE_RATE
        self.click = (np.sin(t * 2 * np.pi * 2000) * np.exp(-t * 130) * 0.4).astype(np.float32)

    def prepare(self, instrument: str, pitch: int = 0):
        if instrument not in INSTRUMENTS:
            raise ValueError(f"未知音色: {instrument}")
        ident = (instrument, pitch)
        if ident not in self.cache:
            samples = []
            for key in range(15):
                path = self.assets / instrument / f"{key}.wav"
                data, rate = sf.read(path, dtype="float32", always_2d=True)
                if not len(data):
                    raise ValueError(f"音色采样为空: {path}")
                data = np.repeat(data, 2, axis=1) if data.shape[1] == 1 else data[:, :2]
                data = resample(data, rate / SAMPLE_RATE * 2 ** (pitch / 12))
                samples.append((data, loop_sample(data)))
            with self.lock:
                # Keep at most four complete banks, including the one about to be used.
                if len(self.cache) >= 4:
                    self.cache.pop(next(iter(self.cache)))
                self.cache[ident] = samples
        with self.lock:
            self.voices.clear()
            self.instrument, self.pitch = instrument, pitch

    def trigger(self, keys: tuple[int, ...], durations: tuple[float, ...] = ()):
        with self.lock:
            samples = self.cache.get((self.instrument, self.pitch))
            if samples is None:
                return
            for i, key in enumerate(keys):
                duration = durations[i] if i < len(durations) else 0
                if self.instrument in SUSTAIN and duration > 0:
                    self.note_off(key)
                    self.voices.append(Voice(samples[key][1], key, loop=True,
                                             remaining_frames=max(1, round(duration * SAMPLE_RATE / 1000))))
                else:
                    self.voices.append(Voice(samples[key][0], key))
            self.voices = self.voices[-96:]

    def restore(self, keys: tuple[int, ...], durations: tuple[float, ...]):
        # Seeking must not add a fresh attack to a plucked/decaying instrument.
        if self.instrument in SUSTAIN:
            self.trigger(keys, durations)

    def note_on(self, key: int):
        with self.lock:
            if self.instrument not in SUSTAIN:
                self.trigger((key,))
                return
            self.note_off(key)
            samples = self.cache.get((self.instrument, self.pitch))
            if samples:
                self.voices.append(Voice(samples[key][1], key, loop=True))

    def note_off(self, key: int):
        with self.lock:
            for voice in self.voices:
                if voice.key == key and voice.loop and voice.release_frames is None:
                    voice.release_frames = 1500

    def pump(self):
        pass

    def finish(self):
        pass  # Let naturally completed samples ring out.

    def silence(self):
        with self.lock:
            self.voices.clear()
            for buf in self.delay_buffers:
                buf.fill(0)

    def render(self, frames: int) -> np.ndarray:
        with self.lock:
            output = np.zeros((frames, 2), np.float32)
            alive = []
            for voice in self.voices:
                if voice.loop:
                    indices = (voice.position + np.arange(frames)) % len(voice.data)
                    block = voice.data[indices].copy()
                    voice.position = (voice.position + frames) % len(voice.data)
                else:
                    count = min(frames, len(voice.data) - voice.position)
                    block = np.zeros((frames, 2), np.float32)
                    block[:count] = voice.data[voice.position : voice.position + count]
                    voice.position += count
                if voice.remaining_frames is not None:
                    # Sample-clock release: invariant to audio callback block size.
                    gain = np.clip((voice.remaining_frames + 1500 - np.arange(frames)) / 1500, 0, 1)
                    block *= gain[:, None]
                    voice.remaining_frames -= frames
                if voice.release_frames is not None:
                    gain = np.clip((voice.release_frames - np.arange(frames)) / 1500, 0, 1)
                    block *= gain[:, None]
                    voice.release_frames -= frames
                output += block
                if (voice.loop or voice.position < len(voice.data)) and (
                    voice.release_frames is None or voice.release_frames > 0
                ) and (voice.remaining_frames is None or voice.remaining_frames > -1500):
                    alive.append(voice)
            self.voices = alive
            if self.cave:
                dry = output.copy()
                for i, buf in enumerate(self.delay_buffers):
                    # Process chunks no longer than a delay line, including callback wrap-around.
                    written = 0
                    while written < frames:
                        pos = self.delay_positions[i]
                        count = min(frames - written, len(buf) - pos)
                        wet = buf[pos : pos + count].copy()
                        buf[pos : pos + count] = dry[written : written + count] + wet * 0.62
                        output[written : written + count] += wet * 0.17
                        self.delay_positions[i] = (pos + count) % len(buf)
                        written += count
            if self.metronome_bpm > 0:
                interval = max(len(self.click), int(SAMPLE_RATE * 60 / self.metronome_bpm))
                phase = (self.metro_frame + np.arange(frames)) % interval
                mask = phase < len(self.click)
                output[mask] += self.click[phase[mask], None]
                self.metro_frame = (self.metro_frame + frames) % interval
            return np.clip(output * self.volume, -1, 1).astype(np.float32)


class QtAudioOutput:
    def __init__(self, mixer: Mixer):
        from PySide6.QtCore import QIODevice
        from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices

        class Stream(QIODevice):
            def __init__(self):
                super().__init__()
                self.open(QIODevice.OpenModeFlag.ReadOnly)

            def isSequential(self):
                return True

            def bytesAvailable(self):
                return 8192 + super().bytesAvailable()

            def readData(self, maxlen):
                frames = maxlen // 8
                return mixer.render(frames).astype("<f4").tobytes()

            def writeData(self, data):
                return -1

        device = QMediaDevices.defaultAudioOutput()
        if device.isNull():
            raise RuntimeError("没有可用的音频输出设备")
        fmt = QAudioFormat()
        fmt.setSampleRate(SAMPLE_RATE)
        fmt.setChannelCount(2)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Float)
        if not device.isFormatSupported(fmt):
            raise RuntimeError("音频设备不支持 44.1 kHz 立体声浮点格式")
        self.stream = Stream()
        self.sink = QAudioSink(device, fmt)
        self.sink.setBufferSize(8192)
        self.sink.start(self.stream)

    def close(self):
        self.sink.stop()
        self.stream.close()
