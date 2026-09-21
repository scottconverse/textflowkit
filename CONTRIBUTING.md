# Contributing to textflowkit

Thanks for considering a contribution. This project is small and the bar is
simple: **don't let a claim outrun its evidence.**

## Setup

Requires **Python ≥ 3.10** and **ffmpeg** on `PATH`.

```bash
git clone https://github.com/scottconverse/textflowkit
cd textflowkit
python -m venv .venv
# Windows: .venv\Scripts\activate
# POSIX:   source .venv/bin/activate
pip install -e ".[dev,mcp,http]"
```

## Test and lint — the commands CI runs

```bash
ruff check .        # must pass with zero findings
python -m pytest    # full suite
```

Both run on every push and pull request via
[`.github/workflows/ci.yml`](.github/workflows/ci.yml). If you can run those two
commands clean, CI should pass.

The test job runs against Python 3.10, 3.11, 3.12, and 3.13. Test on the oldest
version you can if you touch typing or `__future__` imports.

## Working on Whisper / GPU code

`textflowkit` uses `openai-whisper` on PyTorch. On AMD hardware that means a ROCm
build of torch; see [docs/install.md](docs/install.md) for the torch-pinning trap
(installing `openai-whisper` naively will replace a ROCm torch with a CPU wheel).

Most tests do **not** need a GPU or a model download. If you add one that does,
mark it so it can be skipped in CI.

## Pull requests

- Keep the change scoped; state what it fixes.
- Add a test for a bug fix. If you cannot, say why in the PR description.
- Run `ruff check .` and `python -m pytest` before opening the PR.
- Do not commit media files, transcripts, or scratch output. `work/` is gitignored.

## Evidence language

This project was audited and the audit's rule applies to contributions too:

- "Read the file" ≠ "ran the product"
- "Tests exist" ≠ "tests prove the behavior"
- Prefer "I ran X and saw Y" over "should work."

A PR description that overstates what was verified will be sent back. That is the
whole point of the rule — see `docs/` and the repo's audit directive.

## Adding a media platform

Add its domains to `PLATFORMS` in `src/textflowkit/sources/detect.py`. If `yt-dlp`
already supports the site, that is usually the entire change — no pipeline or
renderer code is involved. Add a case to `tests/test_sources.py`.

Read [LEGAL.md](LEGAL.md) before adding a source adapter.

## Security

Do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).
