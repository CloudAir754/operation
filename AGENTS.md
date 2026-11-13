# Repository Guidelines

## Project Structure & Module Organization

LabOpsPanel is a Windows-local web console for managing Linux hosts over SSH.

- `src/` — Python backend, FastAPI routes, SSH transport, remote probes/actions, task orchestration, and persistence.
- `frontend/` — React + TypeScript + Vite browser client.
- `tests/` — backend unit and integration tests.
- `frontend/src/` — frontend code and `*.test.*` tests.
- `docs/requirements/` — product requirements, architecture decisions, and acceptance criteria.
- `assets/` or `public/` — static resources when needed.

Keep feature code grouped by domain. Do not place runtime databases, credentials, SSH keys, build output, or logs in the source tree.

## Build, Test, and Development Commands

Use Python 3.12 and manage the backend with `uv`:

- `uv sync --extra dev` — install or refresh Python dependencies.
- `uv run uvicorn lab_ops_panel.app:app --reload` — run the local API during development.
- `uv run pytest` — run backend tests.
- `uv run ruff check .` — lint Python code.
- `uv run ruff format --check .` — verify Python formatting.
- `npm ci` (from `frontend/`) — install frontend dependencies.
- `npm run dev` — run the Vite development server.
- `npm run typecheck` and `npm test` — check and test the frontend.
- `npm run build` — create the production frontend bundle.

Document any new canonical command in `docs/requirements/README.md`.

## Coding Style & Naming Conventions

Follow Ruff for Python and the repository TypeScript configuration for frontend code. Use four-space indentation in Python and two spaces in TypeScript, JSON, and YAML. Use `snake_case` for Python modules and functions, `PascalCase` for React components and classes, `camelCase` for TypeScript values, and lowercase kebab-case for non-Python filenames. Keep API payloads explicit and reject unknown fields for sensitive operations.

## Testing Guidelines

Add tests for SSH config parsing, DPAPI credential handling, task cancellation and recovery, connection failures, timeouts, service protection, permission degradation, and remote output limits. Tests must use temporary fixtures or fakes; never alter real accounts, sudoers, disks, SSH keys, or production configuration. Keep test names behavior-focused, such as `test_proxyjump_config_is_preserved`.

## Security & Configuration Tips

The application listens on `127.0.0.1` by default. Store application data under the Windows user profile using SQLite; encrypt per-host sudo passwords with Windows DPAPI. Never commit passwords, private keys, session tokens, host fingerprints from private infrastructure, or raw command output. Use Windows OpenSSH configuration as the source of connection settings, preserve `ProxyJump`/`ProxyCommand` behavior, and keep remote actions behind structured allowlists, preflight checks, confirmations, timeouts, and bounded output.

## Documentation & Change Management

Treat `docs/requirements/` as the source of product decisions. Update the relevant requirement and acceptance criteria when behavior changes. Use concise imperative commit subjects such as `Add SSH config importer`. Pull requests should describe affected hosts and UI behavior, list validation commands, identify security-sensitive changes, and include screenshots for visible page changes.


## Git Branching

After completing a focused change, run the relevant checks and review the diff for correctness and sensitive data. When checks pass, automatically create a local commit on the working branch without asking for additional confirmation. If checks fail or cannot run, report the limitation instead of claiming validation passed. Keep unrelated existing changes in separate commits only when the user authorizes saving them. Push branches and create pull requests when requested by the user; do not automatically merge.

Every new feature, bug fix, documentation change, refactor, or other focused change must be developed on its own branch. Use a managed prefix followed by a short kebab-case name, for example `feat/host-import`, `bug-fix/frontend-port`, `docs/update-runbook`, `refactor/task-manager`, or `chore/refresh-dependencies`. Keep the default branch reserved for reviewed, integrated changes; do not make routine implementation commits directly on it.
