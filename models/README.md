# Model artifacts

This directory holds fitted model artifacts. It is **empty in version
control** — `.gitignore` excludes `*.joblib`, because a binary blob in git
is neither reviewable nor reproducible.

See [ADR-0002](../docs/decisions/0002-transparent-credit-model.md) for why the
default is a transparent scorecard rather than a fitted model.

The API does not need anything here. With no artifact present,
`nuvix.credit.engine.registry` serves `LogisticScorecard`, a transparent
hand-calibrated scorecard, and says so in its `model_version`.

To produce a fitted model:

```bash
pip install -r requirements-ml.txt
# Place the Kaggle "Loan Default Prediction" CSV at data/Loan_default.csv
python scripts/train_models.py --data data/Loan_default.csv
export ACTIVE_PD_MODEL=pd-gbdt-v1     # whatever the script reports
```

Each run writes two files:

- `<version>.joblib` — the fitted estimator (a scikit-learn `Pipeline`)
- `<version>.json` — feature order, metrics, training date, row count

The metadata file is what makes an artifact auditable six months later: a
`.joblib` on its own cannot tell you what it was trained on or how well it
scored.
