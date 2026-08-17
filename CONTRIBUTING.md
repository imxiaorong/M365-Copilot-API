# Contributing

Thanks for your interest! Before you invest time in a pull request, please
read the [DISCLAIMER](DISCLAIMER.md) — this project carries inherent legal
risk that you should be aware of.

## Scope

This project is a personal tool, not a commercial product. Contributions that
align with the project's scope are welcome:

- **Bug fixes** — issues with the protocol driver, server, or tooling
- **Protocol improvements** — keeping up with changes to the M365 Copilot
  wire format
- **Documentation** — clarifications, examples, setup tips
- **Modest features** — things that make the tool more useful for personal use

Contributions that are **out of scope**:

- Large-scale or commercial use features
- Circumventing rate limits, authentication, or access controls
- Adding support for other Microsoft services beyond M365 Copilot Chat
- Removing or weakening the `.gitignore` exclusions for `session/` or
  `captures/`

## Before you start

1. **Open an issue first** for feature requests or non-trivial changes —
   don't surprise the maintainer with a 500-line PR out of the blue.
2. For bug fixes, a minimal issue is fine — no need to debate scope.

## Pull request guidelines

- Keep changes focused — one PR = one thing
- Match the existing code style (readability > cleverness)
- Update documentation (README, docstrings) if your change affects usage
- Verify the project still works: `copilot ask "hello"` and `python app.py`
  smoke test
- Do **not** commit `session/`, `captures/`, `venv/`, or `__pycache__/`
  (they are gitignored; double-check before pushing)

## Code of conduct

Be respectful. This is a small side project, not a battleground.