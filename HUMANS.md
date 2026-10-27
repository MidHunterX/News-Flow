# HUMANS.md

Human thought process for planning and developing.

## Abstract

**News Flow** is designed to be an infrastructure which provide automated
abstraction which aids human operators first for better efficiency and tuning;
then pair working or delegated over to AI using the optimized infra.

**For Machines**: Functions that abstract messy human world data and provide
useful **structured** data for doing **deterministic** and rule driven works
which machines are really good at like extraction, management, transformation,
enforcing rules and standardization, temporal logic, etc.

**For Humans**: Seeing Spaces with **visualizations** for human judgement and
decision making, **Workspaces** with useful **tools** and **abstractions** for
time and cognitive energy efficient work. This way humans can spend time and
effort to where humans are really good at and where it actually matters.

**For AI**: Context from the machine space and Actions from the human space
used in combination to pair work with humans using the tools the human operator
uses for doing **Non-Deterministic** works.

## Development Process

### Guardrails

In order to guide AI development, the best way is to set up deterministic rule
based guardrails. Guardrails good at preventing both human and AI from making
mistakes while developing.

- Code Formatters: For consistent code style. `ruff format` over Black + Isort.
- Code Linters: Catch bugs and errors. `ruff check` over Flake8.
- Static Type Checkers: Enforce type safety. `pyrefly` over mypy or Pyright.
  It's 15x faster than both.
- Package Management: Manage dependencies. `uv` over `pip`. It's fast and auto
  manages package actively.
- Testing Frameworks: Test code with `pytest` which is the de-facto standard.
- Git Hooks: Instead of asking AI to use the tools, enforce them with a git
  hook. Use `prek` over `pre-commit`. Since it's written in rust, it's fast and
  can be used on other codebases irrespective of technology.

### Problems Faced

Like developers, AI is also really good at greenfield development and adding
more features on existing codebase. The problem problem starts when maintenance
or changing system behavior.

