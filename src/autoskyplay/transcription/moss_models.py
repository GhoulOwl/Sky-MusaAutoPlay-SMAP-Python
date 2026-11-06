"""Pinned official ModelScope source weights for local MOSS GGUF conversion."""
from .models import ModelStore


class MossSourceStore(ModelStore):
    title = 'MOSS-Music 原始权重'
    prefix = 'moss-source'
    weight_name = 'model-00001-of-00004.safetensors'
    license = 'Apache-2.0'
    catalog = {'moss-source': dict(revision='6699067214b59320df11781911b72563d93d9e54', files={
        'added_tokens.json': (801, '1fabcc983ee0051b922009b1b5a7af1844385f38783e62679ac35d6dd2aa3355'),
        'config.json': (2762, '843491d35bc46236a80b6f9ebaf038db6b7e6c7a7f2ad995267d1b901c9d3bf4'),
        'merges.txt': (1671853, '8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5'),
        'vocab.json': (3383407, '87a257b04b17642a0688c98cd1df89c398bda4fee532d6f88b38a659ecb4ac8d'),
        'model-00001-of-00004.safetensors': (4931219872, '63edf20789c6fa3bac2fb635a097a927a64237d488ffa29c2e1a946c84490f3f'),
        'model-00002-of-00004.safetensors': (4983069688, 'b2473dec1d6a648372beaf906d26477f73e271de8f0c8eaa8d5b7147518c666c'),
        'model-00003-of-00004.safetensors': (4999847576, '5b00d237df0fc68b5d17504ce02dcdae1a724424042d47d6156c5cf60706f4e6'),
        'model-00004-of-00004.safetensors': (3190899504, 'e598e7a6d470f4766639abafef01670dff46cda3c636aea64d0801cc26309584'),
    })}

    def repository(self, size):
        return 'openmoss/MOSS-Music-8B-Instruct'
