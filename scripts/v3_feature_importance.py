"""Extract feature importance from a saved LightGBM model + meta.json.

One-off script for the v3 vs v2 comparison report. Reads the model
file from research.models row given by version, re-runs the LightGBM
``feature_importance(importance_type='gain')`` over the canonical
feature_names list stored in meta.json, and prints the top N pairs.

Usage: python scripts/v3_feature_importance.py <version> [<top_n>]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: v3_feature_importance.py <version> [<top_n>]")
        return 2
    version = sys.argv[1]
    top_n = int(sys.argv[2]) if len(sys.argv) > 2 else 10

    # Locate the model dir by scanning models/last_90s_forecaster_v* paths.
    model_root = Path("models")
    candidates = list(model_root.glob(f"*/{version}"))
    if not candidates:
        print(f"version not found under models/: {version}")
        return 3
    if len(candidates) > 1:
        print(f"ambiguous version (multiple models named {version}): {candidates}")
        return 3
    model_dir = candidates[0]
    meta = json.loads((model_dir / "meta.json").read_text())
    feature_names = list(meta["feature_names"])

    import lightgbm as lgb

    booster = lgb.Booster(model_file=str(model_dir / "model.lgb"))
    importances = booster.feature_importance(importance_type="gain")
    if len(importances) != len(feature_names):
        print(
            f"WARNING: importance length {len(importances)} "
            f"!= feature_names length {len(feature_names)}"
        )

    pairs = sorted(zip(feature_names, importances, strict=False), key=lambda x: -float(x[1]))
    total = sum(float(i) for _, i in pairs) or 1.0
    print(f"# feature importance (gain) — {version} — n_features={len(feature_names)}")
    print(f"{'rank':>4}  {'feature':<28}  {'gain':>12}  {'pct':>6}")
    for i, (name, gain) in enumerate(pairs[:top_n], 1):
        print(f"{i:>4}  {name:<28}  {float(gain):>12.2f}  {float(gain)/total*100:>5.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
