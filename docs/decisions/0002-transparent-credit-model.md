# ADR-0002 — A transparent factor model, not a black box

**Status:** accepted

## Context

The original prototype trained a LightGBM regressor on credit score and a
PyTorch network on default. Both scored respectably. The question was whether
to serve them as the primary scoring model.

## Decision

The **credit score** is an additive five-factor model at the bureaus'
published weights, with no learned component.

**Default risk** is served through a versioned registry, which defaults to a
transparent hand-calibrated logistic scorecard and can serve fitted models
when an artifact is present.

## Reasoning

**Regulation.** Under US fair-lending rules an adverse decision must come with
specific reason codes. A gradient-boosted ensemble can be given SHAP values,
but per-prediction attributions on a tree ensemble are an approximation, and
"approximately why we declined you" is not a defensible position.

**The product is advice, not a verdict.** The score exists so the simulator
can say *"paying $2,500 would add 45 points, and here is which factor moves"*.
That requires a model whose structure is the explanation. A learned model can
predict the bureau's number more closely and cannot say why.

**Honesty about what we have.** Bureau scores are proprietary. A model trained
on public loan data predicts *that dataset's* score column, not FICO. Claiming
otherwise would be the real dishonesty. The factor model aims at something
achievable and stated: a score that **moves the way a real one moves**.

**The ML is not discarded.** `scripts/train_models.py` trains both a boosted
tree and a neural network — as a reviewable, re-runnable script rather than a
notebook — and the registry serves whichever is activated. The prototype's
work is preserved where a learned model is genuinely better suited: ranking
default risk, where no reason code is owed to a customer who was never
declined.

## Consequences

**Good.** Every score decomposes into named factors with plain-English
reasons. The simulator cannot disagree with the model, because it *is* the
model. The API boots with no ML stack installed. Model version is recorded on
every decision.

**Bad.** The factor model will not match a real FICO pull exactly, and the
docstrings say so. The scorecard's coefficients are calibrated, not fitted —
stated plainly in the registry rather than implied to be trained.
