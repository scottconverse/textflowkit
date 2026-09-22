# Install notes

## Requirements

- **Python ≥ 3.10**
- **ffmpeg** on `PATH` (ffprobe comes with it)
- **yt-dlp** — installed as a dependency

## AMD GPU support (ROCm) — no WSL required

textflowkit runs natively on Windows. On AMD hardware, GPU acceleration comes from a
**ROCm build of PyTorch**; no Linux layer or WSL is involved.

This project was developed against AMD Strix Halo (`gfx1151`, Radeon 8060S) using
AMD's `rocm-sdk` pip distribution:

```
rocm 7.13.0 | rocm-sdk-core 7.13.0 | rocm-sdk-libraries-gfx1151 7.13.0
torch 2.11.0+rocm7.13.0 | HIP 7.13.99004
```

Device libraries (`rocblas`, `hipblas`, `MIOpen`, `libhipblaslt`) ship as part of that
distribution for the target architecture.

### The torch-pinning trap

`openai-whisper` depends on `torch` **unpinned**. A plain `pip install openai-whisper`
will happily replace a working ROCm torch with a stock PyPI CPU wheel, silently
disabling GPU acceleration.

Install the engine **without** letting it resolve torch:

```bash
pip install openai-whisper yt-dlp --no-deps
pip install tiktoken more-itertools tqdm numba
```

Keep your existing ROCm torch. Verify before and after:

```bash
python -c "import torch; print(torch.__version__, torch.version.hip, torch.cuda.is_available())"
```

Expected on ROCm: `2.11.0+rocm7.13.0 7.13.99004 True`.

### Notes

- Whisper reports the AMD device as `cuda` — that is PyTorch's HIP-as-CUDA
  compatibility surface, and it is correct.
- Whisper warns `Failed to launch Triton kernels ... falling back to a slower DTW
  implementation` on ROCm. This is **expected and harmless**: Triton is CUDA-only.
  Timing still works via the fallback path; expect a small speed cost on
  word-level timing only.
- `faster-whisper` / CTranslate2 is **not** the right choice on AMD Windows. Its GPU
  path requires CUDA, and ROCm is only available by compiling from source with
  `-DWITH_HIP=ON`.

## yt-dlp resolution

`textflowkit` resolves `yt-dlp` in this order:

1. an executable on `PATH`
2. an executable beside the running interpreter (e.g. the venv's `Scripts/yt-dlp.exe`)
3. **the `yt_dlp` Python module, called in-process**

The third case matters: installing `yt-dlp` as a dependency places an entry point in
the environment, but that directory is not on `PATH` unless the environment is
activated. Without the fallback you would hit
`required tool 'yt-dlp' not found on PATH` even though the dependency is installed.
Verified end-to-end against a public YouTube video.

## JavaScript runtime (YouTube)

Modern `yt-dlp` uses a JavaScript runtime to solve YouTube's signature/n-param
challenges. **Only `deno` is enabled by default**, so a machine that has Node,
bun, or quickjs still reports:

```
WARNING: No supported JavaScript runtime could be found.
```

This is not fatal — extraction falls back to non-JS paths and many videos work —
but some formats become unavailable, which can leave no audio-only stream.

`textflowkit` therefore **detects** what is installed and enables it explicitly, in
yt-dlp's own priority order:

```
deno  ->  node  ->  bun  ->  quickjs
```

- If `deno` is present, no flags are needed (it is yt-dlp's default).
- If another runtime is found, `--no-js-runtimes --js-runtimes <name>` is passed so
  the detected runtime actually takes effect.
- If **none** are found, nothing is passed and yt-dlp's normal fallback applies.

This means **no specific runtime is required**. If you use YouTube heavily and want
the warning gone, installing any one of them is enough — and there is no need to
install Deno if you already have Node.

To check what was detected:

```bash
python -c "from textflowkit.sources.acquire import detect_js_runtime; print(detect_js_runtime())"
```

## Verifying the GPU path

No hosted CI runner has an AMD GPU, so the ROCm path cannot be covered by CI. The
honest substitute is a check you can run anywhere, on demand:

```bash
textflowkit selftest              # compute device + a real tiny transcription
textflowkit selftest --skip-transcribe   # compute device only, no model download
```

It runs a real matmul on the selected device and then a real Whisper pass over a
generated probe clip, printing PASS/FAIL for each and naming the torch build. A
ROCm install reports `torch <ver>+rocm*` and the device name; a stock CPU wheel
reports plain `torch <ver>`.

`--skip-transcribe` is cheap enough to run in CI and is exercised there on every
platform.

## Diarization: the torch-clobber trap (same class as above)

`pip install pyannote.audio` **will replace a ROCm torch with a stock PyPI CPU
wheel** - verified by resolving the dependency: it pulls `torch==2.14.0`, while
this machine runs `2.11.0+rocm7.13.0`. Installing the diarize extra naively
therefore silently destroys GPU acceleration, exactly like the `openai-whisper`
trap earlier in this document.

Install the extra with torch held back:

```bash
pip install "textflowkit[diarize]" --no-deps
pip install pyannote.audio torchaudio torchmetrics torchcodec
```

Then verify with `textflowkit selftest` that the torch build is still the ROCm one.

Note also that the pyannote diarization model is **gated**: it requires a Hugging
Face token with access granted to the model on huggingface.co. Granting access is
a manual step on their site and cannot be automated here.

## CPU fallback

With no GPU, the engine selects CPU automatically. Pass `--device cpu` to force it.
CPU transcription is dramatically slower; prefer a smaller `--model`.


