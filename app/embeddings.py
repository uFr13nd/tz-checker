"""Локальные эмбеддинги без внешнего провайдера.

Хэширующие векторы по стемам слов и символьным 4-граммам. Это не
нейросетевая семантика, но устойчивый дополнительный сигнал, который
работает всегда — даже если провайдер не поддерживает /embeddings.
"""

from __future__ import annotations

import hashlib
import math
import re

from app.textutils import STOP_WORDS, fold, stem, tokenize

LOCAL_MODEL = "local-hash-v1"
LOCAL_DIM = 256


def local_embedding(text: str, dim: int = LOCAL_DIM) -> list[float]:
    vector = [0.0] * dim
    normalized = fold(text or "")
    features: dict[str, int] = {}
    for word in tokenize(normalized):
        if word in STOP_WORDS:
            continue
        key = "w:" + stem(word)
        features[key] = features.get(key, 0) + 1
    compact = re.sub(r"[^0-9a-zа-я]+", "", normalized)
    for index in range(max(0, len(compact) - 3)):
        key = "g:" + compact[index : index + 4]
        features[key] = features.get(key, 0) + 1
    for feature, count in features.items():
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        position = int.from_bytes(digest[:4], "little") % dim
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[position] += sign * min(count, 5)
    norm = math.sqrt(sum(value * value for value in vector))
    if norm:
        vector = [value / norm for value in vector]
    return vector
