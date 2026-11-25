# Chano

> A lightweight Vercel serverless Python framework

## Required skills

- Jujutsu for version control: ./.agents/skills/jujutsu/SKILL.md
- lat.md for project wiki: ./.agents/skills/lat-md/SKILL.md

## Useful commands

- uv run pytest
- uv run pytest -m "not slow"
- uv run ty check
- uv run ruff check .
- uv run ruff format --check src tests examples site/build.py
- lat check
- gh actions-lock (updates `.github/workflows/actions.lock` after editing GitHub Actions pins)
- uvx zizmor . (audits `.github/workflows/` and the lockfile)
