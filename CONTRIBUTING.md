# Contributing

Sinas Grounded Research is in alpha. The architecture is settling, the schema migrations
will collapse before 1.0, and the package contract with Sinas is still moving.
That said, feedback while it's in motion is more useful than feedback after.

## Filing issues

- **Bugs**: include the SGR version (`git rev-parse HEAD`), the Sinas
  version, the relevant section of `docker logs sinas-grounded-research-sgr-1`, and what
  you expected to happen.
- **Behavioral questions** ("should this work like X?"): open an issue —
  decisions taken in chats won't make it into the repo.
- **Security issues**: email rather than file publicly. See the repo profile.

## Pull requests

- Branch off `main`, keep PRs focused.
- Don't bother with extensive docs updates for in-flight features — README
  drift is expected.
- Run `poetry run ruff check . && poetry run black .` before pushing.
- Frontend: `cd frontend && npm run lint`.

## What's stable enough to build on

- The data model layout (document/document_version/property_value/etc.) — the
  shapes are spec'd in `ARCHITECTURE.md`.
- The package install flow with Sinas's variable-substitution feature.

## What's NOT stable

- Permission strings.
- The connector operation set (still adding/renaming).
- The agent system prompts (being tuned).
- The post-upload extraction path (which converter, what fallbacks).
