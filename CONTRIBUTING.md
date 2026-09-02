# Contributing

Thanks for your interest in improving this sample. It exists to demonstrate
RavenDB as in-cluster working memory for an LLM agent, so contributions that
keep that story clear and correct are especially welcome.

## Reporting issues

Open an issue at https://github.com/ravendb/samples-hidden-city/issues with:

- What you expected to happen and what happened instead
- Steps to reproduce (which mode: `start.ps1 -Mode Local` or `-Mode K8s`)
- Relevant logs (`kubectl logs`, agent/worker output) or a stack trace

## Submitting changes

1. Fork the repository and create a branch from `main`
2. Make your change
3. Run `pytest tests/unit/` (and `pytest tests/integration/ --require-ravendb`
   if you have a RavenDB instance available) before opening a PR
4. Open a pull request describing the change and why it's needed

## Guidelines

- Follow the conventions and constraints documented in [`CLAUDE.md`](CLAUDE.md)
  — in particular, RavenDB must stay an explicit LLM tool call, never a
  sidecar that blindly injects context, and conversation memory belongs in
  RavenDB, not pod RAM or Redis.
- Python code uses type hints and Pydantic models at all inter-service
  boundaries — no raw dicts passed between agent, tools, and memory.
- Keep the hidden-city detection flow informational only; do not add code
  that automates booking (see [`docs/hidden-city.md`](docs/hidden-city.md)).
- Config comes from Kubernetes ConfigMaps/Secrets or `.env` for local dev —
  never hardcode credentials.

## License

By contributing, you agree that your contributions will be licensed under
the [MIT License](LICENSE) that covers this project.
