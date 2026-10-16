# Contributing

Thanks for looking. Pelago OS is small and young; contributions of any size
are welcome, from typo fixes to new features.

## Getting set up

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check app scripts tests skills
```

All tests must pass and `ruff` must be clean before a pull request is merged.

## How we work

- **Small pull requests.** One change, one reason. Explain the why in the
  description.
- **Tests with the change.** Bug fixes come with a test that failed before.
- **Security first.** Anything touching auth, permissions, uploads or the
  library needs a short note on what could go wrong. See `SECURITY.md`.
- **No secrets, no personal data** in code, tests, fixtures or screenshots. Use
  `example.com` addresses and demo names like `reset_guy`.
- **AI-assisted contributions are welcome**, which is rather the point of the
  project. You are still responsible for what you submit: read it, run it.

## Language

The interface is currently in Chinese. Code, comments and docs may be in
English or Chinese; English is preferred for anything new in the docs.
Translation help is very welcome.

## License

By contributing you agree that your contributions are licensed under the
Apache License 2.0.
