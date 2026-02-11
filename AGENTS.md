
> Summary of Project: It is a local-first, tool-augmented LiveKit voice assistant platform that already handles conversational help and personal workflow actions and is evolving toward a dedicated dictation mode with LLM-based real-time transcription cleanup, autocorrection, and writing enrichment.

# AGENTS.md
Operational guide for coding agents working in `livekit-agent`.

## Scope
- Applies to the full repository rooted at `livekit-agent/`.
- If nested `AGENTS.md` files are added later, the deepest file wins for its subtree.

## Repository Reality Check
- `CLAUDE.md` is legacy guidance and reflects an older project structure.
- Prefer the current filesystem layout and `pyproject.toml` as the source of truth.
- If `CLAUDE.md` conflicts with code in `agents/`, `services/`, or current configs, follow the code/config.

## Stack and Architecture
- Language: Python (`>=3.11,<3.14`, from `pyproject.toml`).
- Package manager + runner: `uv`.
- Framework: LiveKit Agents with OpenAI/Silero/turn-detector/MCP extras.
- Primary app modules:
  - `agents/livekit_basic_example.py`
  - `agents/livekit_general_agent.py`
- Service modules:
  - `services/local_stt_server.py`
  - `services/service_daemon.py`
  - `services/daemon_client.py`
  - `services/docker_mcp_gateway.py`
  - `services/mcp_gateway.py`
- Deployment config: `livekit.toml`.

## Environment Setup
```bash
uv sync
uv sync --extra dev
cp .env.example .env
```
- Common variables used in this repo:
  - `OPENAI_API_KEY`
  - `DEEPGRAM_API_KEY` (if using Deepgram paths)
  - `OLLAMA_BASE_URL`, `LOCAL_STT_BASE_URL`, `KOKORO_BASE_URL`
  - Optional: `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`

## Build, Lint, and Test Commands
There is no dedicated compile/build pipeline here; use these quality gates.
### Lint
```bash
uv run ruff check .
```
### Format
```bash
uv run black .
```
### Type Check
```bash
uv run mypy agents services
```
### Tests (full)
```bash
uv run pytest
```
### Run a Single Test (important)
```bash
# one file
uv run pytest tests/test_example.py
# one function
uv run pytest tests/test_example.py::test_specific_behavior
# one class method
uv run pytest tests/test_example.py::TestAgentFlow::test_handoff
# filtered subset
uv run pytest -k "handoff and not slow"
# async debug mode
uv run pytest -v --asyncio-mode=auto
```
### Runtime Smoke Commands
```bash
# basic assistant
uv run python agents/livekit_basic_example.py console
# general assistant
uv run python agents/livekit_general_agent.py console
```

## Tooling Configuration from `pyproject.toml`
- Black line length: `100`
- Ruff line length: `100`, target version `py311`
- Ruff lint families enabled: `E`, `F`, `I`, `N`, `W`
- `E501` is ignored (line-length check delegated to Black)
- Pytest:
  - `asyncio_mode = auto`
  - test paths: `tests`
  - file patterns: `test_*.py`, `*_test.py`
- Mypy:
  - `python_version = 3.11`
  - `warn_return_any = true`
  - `warn_unused_configs = true`
  - `ignore_missing_imports = true`

## Code Style Guidelines
### Imports
- Order imports as: standard library, third-party, local modules.
- Separate groups with one blank line.
- Prefer explicit imports; avoid wildcard imports.
- Keep `from __future__ import annotations` at the top when used.
### Formatting
- Follow Black defaults with max line length 100.
- Use 4-space indentation; no tabs.
- Keep functions focused and avoid deep nesting when possible.
- Add comments only for non-obvious behavior or constraints.
### Types and Interfaces
- Add type hints for public functions and methods.
- Prefer modern built-in generics (`list[str]`, `dict[str, Any]`).
- Keep `Any` at integration boundaries (SDK responses, JSON payloads).
- Preserve compatibility with configured mypy settings.
### Naming Conventions
- `snake_case`: functions, variables, module names.
- `PascalCase`: classes and dataclasses.
- `UPPER_SNAKE_CASE`: constants, especially env-derived values.
- Use descriptive function names for readiness/state transitions.
### Async and Concurrency
- Prefer `async def` in agent and network I/O flows.
- Do not block the event loop in async code.
- Use `asyncio.to_thread` for blocking sync work when needed.
- Use `asyncio.Lock` for shared async state.
- Use `threading.Lock` in multi-threaded daemon sections.
### Error Handling
- Fail fast for startup/readiness failures with clear `RuntimeError` messages.
- Use defensive `try/except` around subprocess, network, and file boundaries.
- Return structured API errors for service endpoints.
- Use `finally` for cleanup (temp files, clients, process handles).
- Prefer retry/poll patterns for transient startup conditions.
### Logging and Diagnostics
- Keep logs concise but actionable.
- Include context (service stage, endpoint, timeout, log location) on failures.
- Never log secrets or credentials.
### LiveKit Agent Patterns
- Build assistants with `Agent` + `AgentSession`.
- Register tools using `@function_tool` and clear docstrings.
- Keep STT/LLM/TTS/turn-detection wiring explicit in session setup.
- Preserve interruption-friendly behavior for voice interactions.
### Configuration and Secrets
- Read config from env vars with sensible defaults.
- Follow existing loading order where used: `.env.local` then `.env`.
- Never hardcode or commit secret keys.
### Platform Notes
- Repository behavior is Windows-oriented in several service paths.
- Prefer `pathlib.Path` for path operations.
- Follow existing hidden-process subprocess patterns on Windows.

## Workflow Expectations for Agents
- Make minimal, targeted changes unless user asks for refactor.
- Run relevant lint/type/test commands before handoff.
- Add focused tests for new behavior when practical.
- For service startup changes, run at least one console smoke check.
- Do not commit `.env` or other secrets.

## Quality Checklist Before Handoff
- If code changed: run at least `ruff check` and targeted tests.
- If interfaces changed: run `mypy agents services`.
- If service startup changed: run one console smoke command.
- Include short validation notes in your handoff message.

## Cursor/Copilot Rules Discovery
No additional repo rules were found at time of writing:
- No `.cursor/rules/`
- No `.cursorrules`
- No `.github/copilot-instructions.md`
If these files are added later, treat them as mandatory supplemental guidance.
