# Datasets

Training data is **not committed**. `scripts/train_models.py` expects the
public "Loan Default Prediction" CSV — the same data the original NuviX
prototype's notebooks used — at `data/Loan_default.csv`.

The API never reads this directory. With no trained artifact present,
`nuvix/credit/engine/registry.py` serves a transparent scorecard and reports
`model_version: "pd-baseline-v1"` so the fallback is visible rather than
silent.
