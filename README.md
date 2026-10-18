# ossbot

A self-hosted bot that finds, implements and validates open-source contributions and publishes them
only after explicit human approval on Telegram. The design is `docs/DESIGN.md`; setup and the
command reference arrive with the documentation pass (FEAT-011).

## Models

All model choices live in `config/models.yaml` (the global model switch, DESIGN §16). Nothing else
in the repository names a model id, and a unit test enforces this.

- `default` and the pipeline roles: claude-opus-5.5 where deep reasoning is needed (planner, scout,
  coder), claude-sonnet-5.5 elsewhere (vetter, reviewer, summarizer, chat, vision). The reviewer
  must use a different model from the coder. If no distinct model is available, the pipeline
  pauses instead of reviewing with the coder's model.
- `test`: the model of every live check that sends a prompt (`ossctl kiro smoke`, the doctor's live
  checks, the safety probes, live round-trips and tests). It uses the cheapest catalog model and
  `fallback: none`, so a missing test model is a clear error, never a silent switch to an expensive
  model. `test_vision` inherits `test` for the live image check and can be pointed at an
  image-capable model on its own.
- Each role may set `fallback: none`, an explicit list of ids, or nothing (inherit, else the global
  `fallbacks`).

Switch any role, including `test`, with `ossctl model set <role> <catalog id> [--effort e]` or the
Telegram `/model` picker. The change is validated against the Kiro catalog, audited and announced.
`ossctl model list` shows what each role resolves to.
