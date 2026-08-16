# Contributing

Thanks for your interest in Eidetic! Please read the following guidelines before submitting a contribution.

## Development environment

```bash
git clone https://github.com/rongshenCarson/eidetic-memory && cd eidetic-memory
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e . --no-deps
.venv/bin/pip install pytest   # test dependency
```

## Code style

- Python 3.11+, PEP 8
- Every new feature must ship with tests (`tests/`)
- Run the full test suite before committing: `.venv/bin/python -m pytest tests/ -q` (57 tests)

## Filing an issue

- Bug reports should include: reproduction steps, expected behavior, actual behavior, environment (OS/Python version)
- Feature requests should state: what problem it solves, usage scenario

## Submitting a PR

1. Fork the repository and create a feature branch
2. Make changes + add tests
3. Verify locally: pytest + `eidetic doctor`
4. Open a PR describing the changes and verification results

## License

This project is licensed under the Apache 2.0 License. By submitting a contribution you agree that your contribution is released under Apache 2.0.
