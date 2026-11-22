# hello

A minimal Chano app. `pyproject.toml` depends on `chano` at runtime and adds
`chano[dev]` to `[dependency-groups].dev` so `chano dev` has `uvicorn`
locally while `chano build` omits it from built functions.

```sh
uv run chano dev      # http://127.0.0.1:8000
uv run chano build    # writes .vercel/output
vercel deploy --prebuilt
```
