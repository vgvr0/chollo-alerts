# Contributing

## Development requirements

- Python 3.11, 3.12, or 3.13. The package declares `requires-python = ">=3.11"`.
- [uv](https://docs.astral.sh/uv/) for environment and dependency management.
- Docker and Docker Compose are optional and are only needed for the container
  workflow.

Create the locked development environment with:

```text
uv sync --frozen --extra dev
```

Run the checks from the repository root:

```text
uv run pytest
uv run pytest --cov=chollometro_alerts --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pip-audit
```

Coverage is configured with a minimum of 85% in `pyproject.toml`. Functional
changes should include or update focused tests.

## Branches and pull requests

Work on a branch based on `master`, using a concise purpose such as
`fix/...`, `chore/...`, or `docs/...`. Keep pull requests small and focused,
describe the behavioural change, and include the relevant validation results.

The existing history uses concise imperative subjects and sometimes prefixes
them with `fix:` or `chore:`; follow that style when it is useful. There is no
separate commit-message enforcement configured in this repository.

## Secrets and runtime data

Never commit `.env`, tokens, credentials, private keys, SQLite databases,
backups, logs, or other runtime data. Start from `.env.example` and keep real
values in the local environment. SQLite files and operational state are local
runtime data, not source artifacts.

## Docker

The supported local container workflow is:

```text
docker compose up -d --build
docker compose ps
docker compose logs --tail=100
docker compose exec scanner chollometro-alerts health
```

The Compose services share the named `chollometro-data` volume. Do not use
`docker compose down -v` unless you intentionally want to remove that runtime
data.
