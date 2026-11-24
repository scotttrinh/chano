# Chano Knowledge Graph

High-level architecture, domain rules, design decisions, and test specifications for Chano, a serverless-first Python web framework.

- [[overview]] — Purpose, serverless-first model, and core design decisions
- [[config]] — `[tool.chano]` and `[[tool.vercel.workflows]]` project config, `base_path`, and full request paths
- [[routing]] — File-system routing, specificity ordering, and ASGI request handling
- [[responses]] — Buffered, streaming and server-sent event responses
- [[params]] — Signature-driven parameter resolution, validation, and dependencies
- [[vercel-primitives]] — Vercel Queues, Schedules, and Workflows integration
- [[build]] — Vercel Build Output API v3 bundling and static OpenAPI generation
- [[tests]] — Test specifications anchored to the test suite
