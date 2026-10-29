"""Six useful tracks: lead vocals and five instruments, with backing excluded."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import soundfile as sf

from ..score import atomic_json
from .decoding import DECODING_POLICY, SOURCE_NAMES
from .devices import DEVICE_POLICY
from .engine import Engine
from .events import EventResult
from .game_models import GAME_MODELS, GAME_REVISION
from .hybrid import StageCache, audio_digest, copy_file, prune_hybrid_audio
from .models import MODELS, RUNTIME_REVISION, digest
from .multistem_models import ASSETS

VERSION = 'multistem-1'
PITCHED_STEMS = ('bass', 'guitar', 'piano', 'other')
SEPARATED_STEMS = ('original', 'vocals', 'drums', *PITCHED_STEMS)


def run_multistem(task):
    work, config = task.work, task.engine.config
    models = Path(config['separation_models'])
    commands = config['commands']
    request = json.loads((work / 'request.json').read_text(encoding='utf-8'))
    source_sha = digest(work / 'source.audio', task.cancelled.is_set)
    profiles, identities, hits, timings = {}, {}, {}, {}

    def stage(name, identity, folder, files, command, *, backend='legacy', device='auto', extra=()):
        task.check_cancelled()
        folder.mkdir(exist_ok=True)
        identity = dict(identity, runtime_component=config.get('runtime_identity', task.engine.version))
        cache = StageCache(task.directory, name, identity, task.cancelled.is_set)
        identities[name] = dict(key=cache.key, identity=cache.identity)
        profile_name = f'stage-{name}.json'
        cached = (*files, profile_name)
        began = time.perf_counter()
        with cache.lock():
            hits[name] = cache.restore(folder, cached)
            task.check_cancelled()
            if not hits[name]:
                profile = task._run_engine(Engine(command, task.engine.models if backend in ('game', 'muscriptor')
                                                  else models, backend=backend, model=task.engine.model,
                                                  device=device), folder, list(extra))
                task.check_cancelled()
                if 'events.json' in files:
                    EventResult.load(folder / 'events.json')  # never publish a malformed branch
                wave_names = [n for n in files if n.endswith('.wav')]
                if wave_names:
                    reference_path = folder / ('original.wav' if name == 'separation' else wave_names[0])
                    reference_info = sf.info(reference_path)
                    for wave_name in wave_names:
                        info = sf.info(folder / wave_name)
                        if (info.frames != reference_info.frames or info.frames < 4410
                                or info.samplerate != 44100 or info.channels != 2):
                            raise ValueError('分轨输出长度或格式无效，不写入缓存')
                        audio_digest(folder / wave_name, task.cancelled.is_set)
                atomic_json(folder / profile_name, profile)
                cache.publish(folder, cached)
        profiles[name] = json.loads((folder / profile_name).read_text(encoding='utf-8'))
        if hits[name] and name != 'features':
            from .task import cached_device_event
            task.emit('device', **cached_device_event(profiles[name], device, name))
        timings[name] = time.perf_counter() - began

    task.emit('separate', message='分离人声、鼓、贝斯、吉他、钢琴和其他')
    stage('separation', dict(source_sha256=source_sha, request=request, model=ASSETS['demucs'],
                            worker=VERSION, segment=4, overlap=.25, outer_seconds=30, margin_seconds=1,
                            shifts=0, seed=0, float=True, device=task.engine.device, device_policy=DEVICE_POLICY),
          work, (*[n + '.wav' for n in SEPARATED_STEMS], 'separation.json'), commands['separate'],
          backend='separate', device=task.engine.device, extra=['--separate-only', '--separation-model', 'six'])
    duration = sf.info(work / 'original.wav').duration
    samples = sf.info(work / 'original.wav').frames
    pcm, hashes = {}, {}

    def fingerprint(name):
        path = work / f'{name}.wav'
        info = sf.info(path)
        if info.samplerate != 44100 or info.channels != 2 or info.frames != samples:
            raise ValueError('分轨长度或采样格式不一致')
        pcm[name] = audio_digest(path, task.cancelled.is_set)
        hashes[name] = digest(path, task.cancelled.is_set)

    for name in SEPARATED_STEMS:
        fingerprint(name)
    vocal_work = work / 'vocal_split'
    vocal_work.mkdir(exist_ok=True)
    copy_file(work / 'vocals.wav', vocal_work / 'vocals.wav')
    task.emit('separate', message='从混合人声中分离主唱，排除和声')
    stage('vocal_split', dict(input_pcm_sha256=pcm['vocals'], model=ASSETS['karaoke'], worker=VERSION,
                             device=task.engine.device, output='shared-float-residual-lead'),
          vocal_work, ('lead_vocals.wav', 'backing_vocals.wav', 'vocal-split.json'), commands['vocal_split'],
          backend='vocal_split', device=task.engine.device)
    for name in ('lead_vocals', 'backing_vocals'):
        copy_file(vocal_work / f'{name}.wav', work / f'{name}.wav')
        fingerprint(name)
    # Caller-supplied duration may describe only the selected range (CLI/import).
    # Only the actual source file can prove that we reached the song's end.
    try:
        full_duration = sf.info(work / 'source.audio').duration
    except (OSError, RuntimeError):
        full_duration = None
    scope = dict(starts_song=not task.audio.is_trial and request['start'] <= .001,
                 ends_song=not task.audio.is_trial and full_duration is not None
                 and request['start'] + duration >= full_duration - .1)
    analysis_request = dict(multistem=True, detector=config.get('beat_detector', 'auto'), **scope)
    atomic_json(work / 'analysis-request.json', analysis_request)
    beat_file = models / ASSETS['beat']['filename']
    task.emit('convert', message='建立共享拍点、小节首拍和调性候选')
    stage('features', dict(stems={n: pcm[n] for n in (*SEPARATED_STEMS, 'lead_vocals')},
                           analysis=analysis_request, worker=VERSION,
                           beat_sha256=digest(beat_file) if beat_file.is_file() else None),
          work, ('features.json',), commands['features'])
    reference = json.loads((work / 'features.json').read_text(encoding='utf-8'))
    branches = {}
    game_work = work / 'game'
    game_work.mkdir(exist_ok=True)
    copy_file(work / 'lead_vocals.wav', game_work / 'vocals.wav')
    game_settings = config.get('game', {})
    atomic_json(game_work / 'request.json', dict(game=game_settings, audio_source='lead_vocals'))
    task.emit('pitch', source='GAME', message='识别去和声后的主唱')
    stage('game', dict(input_pcm_sha256=pcm['lead_vocals'], model=GAME_MODELS['medium'],
                       runtime=GAME_REVISION, settings=game_settings, worker=VERSION,
                       device=config.get('game_device', task.engine.device), device_policy=DEVICE_POLICY),
          game_work, ('events.json',), commands['game'], backend='game',
          device=config.get('game_device', task.engine.device))
    branches['lead_vocals'] = EventResult.load(game_work / 'events.json')
    for name in PITCHED_STEMS:
        task.check_cancelled()
        folder = work / name
        folder.mkdir(exist_ok=True)
        copy_file(work / f'{name}.wav', folder / 'source.audio')
        atomic_json(folder / 'request.json', dict(start=0, length=duration, audio_source=name,
                                                 input_origin_seconds=task.start_seconds, input_scale=1))
        task.emit('pitch', source=name, message=f'独立识别{SOURCE_NAMES[name]}')
        stage('instrument-' + name, dict(input_pcm_sha256=pcm[name], model=MODELS[task.engine.model],
                                        runtime=RUNTIME_REVISION, worker=VERSION,
                                        device=task.engine.device, device_policy=DEVICE_POLICY,
                                        decoding=DECODING_POLICY, input_scale=1),
              folder, ('events.json',), commands['background'], backend='muscriptor', device=task.engine.device)
        branches[name] = EventResult.load(folder / 'events.json')
    if any(abs(b.duration_ms - duration * 1000) > 2 for b in branches.values()):
        raise ValueError('分支识谱范围不一致，本次不提交结果')
    # An audition mix is never an inference input. Preserve shared amplitude.
    background = np.zeros((samples, 2), dtype=np.float32)
    for name in ('drums', *PITCHED_STEMS):
        audio, _ = sf.read(work / f'{name}.wav', dtype='float32', always_2d=True)
        background += audio
    sf.write(work / 'background.wav', background, 44100, subtype='FLOAT')
    notes = []
    for name, branch in branches.items():
        for n in branch.raw_notes:
            if n.audio_source != name:
                raise ValueError('识谱分支来源与输入不符')
            notes.append(replace(n, index=len(notes)))
    lead = branches['lead_vocals']
    metadata = dict(backend='hybrid', engine=VERSION, pipeline='multistem', source_sha256=source_sha,
                    selection=request, stem_sha256=hashes, stem_pcm_sha256=pcm,
                    excluded_sources=['backing_vocals'], pitched_sources=list(PITCHED_STEMS),
                    branches={n: b.metadata for n, b in branches.items()},
                    cache_hits=hits, cache_identities=identities, stage_profiles=profiles,
                    timings_seconds=timings, duration_ms=duration * 1000,
                    bpm_estimated=reference['bpm_estimated'],
                    rhythm_analysis=reference['rhythm_analysis'], key_analysis=reference['key_analysis'])
    analysis = dict(reference['analysis'], game_regions=lead.analysis.get('game_regions', []),
                    game_window_review=lead.analysis.get('game_window_review', []))
    result = EventResult(notes, duration * 1000, reference['bpm'], metadata, analysis)
    from .hybrid_evidence import annotate_vocal_boundaries
    annotate_vocal_boundaries(result, work / 'lead_vocals.wav', task.cancelled.is_set)
    task.check_cancelled()
    result.save(work / 'events.json')
    try:
        prune_hybrid_audio(task.directory, protected={identities[s]['key'] for s in ('separation', 'vocal_split')})
    except OSError:
        task.emit('notice', message='派生音频缓存清理未完成；识别结果已保留')
    return result
