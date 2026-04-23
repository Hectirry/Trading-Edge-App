"""4-state Gaussian HMM regime detector (ADR 0012).

Pure-Python wrapper around a pickled ``hmmlearn.hmm.GaussianHMM``.
Training lives in ``trading.cli.train_hmm_regime`` — at runtime the
strategy loads the pickle and calls ``predict()`` with recent 5m
Binance candles.

States are re-labelled post-fit by sorting on ``(mean_return, mean_vol)``
so the runtime label is stable across refits:

- highest mean_vol → ``high_vol``
- low vol + mean_return > 0 → ``trending_bull``
- low vol + mean_return < 0 → ``trending_bear``
- remaining → ``ranging``
"""

from __future__ import annotations

import math
import pickle
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Regime = Literal["trending_bull", "trending_bear", "ranging", "high_vol"]


@dataclass(frozen=True)
class RegimeState:
    label: Regime
    posteriors: tuple[float, float, float, float]  # in canonical label order
    transition_stability: float  # 1 - avg off-diag hmm.transmat_ over states


def yz_volatility(closes: Sequence[float], window: int = 20) -> float:
    """Close-to-close log-return stdev over the last ``window`` bars.

    Yang-Zhang decomposes into open/high/low/close when available, but
    the engine-side caller may only have closes. For stability we return
    the canonical close-to-close std which matches the YZ limit when
    OHLC collapse to a single value.
    """
    if window < 2 or len(closes) < window + 1:
        return 0.0
    tail = closes[-(window + 1) :]
    rets: list[float] = []
    for i in range(1, len(tail)):
        a, b = tail[i - 1], tail[i]
        if a <= 0 or b <= 0:
            continue
        rets.append(math.log(b / a))
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(max(var, 0.0))


def build_feature_matrix(closes: Sequence[float]) -> list[list[float]]:
    """Map a close series to the ``[log_return, yz_vol_20]`` matrix the
    HMM was trained on. Returns list-of-lists to avoid a hard numpy
    dependency in the strategy path.
    """
    out: list[list[float]] = []
    for i in range(1, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        if prev <= 0 or cur <= 0:
            continue
        r = math.log(cur / prev)
        window = closes[max(0, i - 20) : i + 1]
        v = yz_volatility(window, window=min(20, len(window) - 1))
        out.append([r, v])
    return out


def canonical_label_order(
    means: list[tuple[float, float]],
) -> list[Regime]:
    """Given per-state ``(mean_return, mean_vol)`` assign canonical labels.

    Returns a list of labels aligned to the original HMM state index.
    """
    n = len(means)
    if n != 4:
        # Degrade: return opaque labels for non-4 models; callers should
        # only use this helper at training time.
        return ["ranging"] * n  # type: ignore[return-value]
    by_vol = sorted(range(n), key=lambda i: means[i][1])
    low3 = by_vol[:3]
    high1 = by_vol[3]
    sorted_low = sorted(low3, key=lambda i: means[i][0])
    labels: list[Regime] = ["ranging"] * n  # type: ignore[assignment]
    labels[sorted_low[0]] = "trending_bear"
    labels[sorted_low[2]] = "trending_bull"
    labels[sorted_low[1]] = "ranging"
    labels[high1] = "high_vol"
    return labels


class HMMRegimeDetector:
    """Frozen HMM loaded from disk. Construct with a pickled bundle:
    ``{"model": GaussianHMM, "labels": [Regime, ...]}``.
    """

    def __init__(self, model: object, labels: list[Regime]) -> None:
        self._model = model  # hmmlearn.hmm.GaussianHMM
        self._labels = labels

    @classmethod
    def load(cls, path: Path) -> HMMRegimeDetector:
        with open(path, "rb") as f:
            bundle = pickle.load(f)
        return cls(model=bundle["model"], labels=list(bundle["labels"]))

    def predict(self, closes: Sequence[float]) -> RegimeState | None:
        x = build_feature_matrix(closes)
        if len(x) < 21:
            return None
        import numpy as np

        arr = np.asarray(x, dtype=np.float64)
        posteriors = self._model.predict_proba(arr)[-1]
        state_idx = int(posteriors.argmax())
        label = self._labels[state_idx]
        # Canonical posteriors ordered as [bull, bear, ranging, high_vol]
        canonical = {"trending_bull": 0, "trending_bear": 0, "ranging": 0, "high_vol": 0}
        for i, l_ in enumerate(self._labels):
            canonical[l_] = float(posteriors[i])
        ordered = (
            canonical["trending_bull"],
            canonical["trending_bear"],
            canonical["ranging"],
            canonical["high_vol"],
        )
        # transition_stability: 1 - avg off-diagonal mass
        trans = self._model.transmat_
        off_diag = float(trans.sum() - trans.trace()) / max(trans.shape[0], 1)
        stability = max(0.0, 1.0 - off_diag / max(trans.shape[0], 1))
        return RegimeState(label=label, posteriors=ordered, transition_stability=stability)


class NullHMMRegimeDetector:
    """Placeholder used when no model is available. Always returns
    ``None`` so strategies can SKIP with reason ``no_regime``.
    """

    def predict(self, closes: Sequence[float]) -> RegimeState | None:
        return None
