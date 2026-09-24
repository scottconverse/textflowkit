# Install notes

Current release: [v0.1.5](https://github.com/scottconverse/textflowkit/releases/tag/v0.1.5).
Use `textflowkit --version` to confirm the installed version. For everyday
commands and outputs, start with the [user manual](user-manual.md).

## Requirements

- **Python ≥ 3.10**
- **ffmpeg** on `PATH` (ffprobe comes with it)
- **yt-dlp** — installed as a dependency

For a standard CPU or NVIDIA environment, install from
[PyPI](https://pypi.org/project/textflowkit/) with
`python -m pip install textflowkit`. For an existing AMD ROCm environment,
follow the instructions below instead of allowing pip to replace your torch.

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

Install textflowkit and the engine **without** letting either resolve torch:

```bash
pip install textflowkit --no-deps
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

### Build tooling is separate from the ROCm runtime

The installed ROCm torch 2.11.0 declares `setuptools<82`. This conflicts with
the requirement to use `setuptools>=83` in the development/build environment
(the older line has a reported security issue). Do not force 83+ into the ROCm
runtime and leave a broken dependency graph. Use a separate clean build venv;
on Windows:

```powershell
python -m venv work/build-venv
.\work\build-venv\Scripts\python.exe -m pip install "setuptools>=83" build hatchling
.\work\build-venv\Scripts\python.exe -m build --wheel --sdist
```

The build environment does not install torch or process untrusted archives.
Keep the ROCm runtime at a torch-compatible setuptools version until AMD's
torch package relaxes its dependency; verify it with `python -m pip check`.

## yt-dlp resolution

`textflowkit` now uses the **`yt_dlp` Python module** for URL acquisition. It is
a declared dependency, so no separate `yt-dlp` executable is required. The
in-process path lets textflowkit check selected media URLs before download and
recheck returned request URLs. Production URL jobs also require an external
SSRF-filtering egress proxy. The path was verified against a public YouTube
video.

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
- If another runtime is found, it is enabled in the yt-dlp Python API options.
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
bundled synthetic speech clip. It fails if Whisper returns no nonempty, timed
speech segment. A PASS proves text generation, not transcription accuracy.
It prints PASS/FAIL for each stage and names the torch build. A
ROCm install reports `torch <ver>+rocm*` and the device name; a stock CPU wheel
reports plain `torch <ver>`.

`--skip-transcribe` is cheap enough to run in CI and is exercised there on every
platform.

## Diarization

Diarization is opt-in and needs three separate things, all verified on this
machine (Windows, ROCm torch 2.11.0+rocm7.13.0, `pyannote.audio` 4.0.7).

### 1. Pick the version that matches your torchaudio

`pyannote.audio` 3.4.x calls `torchaudio.AudioMetaData`, which no longer exists
in torchaudio 2.11 - importing 3.4.0 raises `AttributeError: module 'torchaudio'
has no attribute 'AudioMetaData'`. **4.x removed that dependency** and loads
cleanly. Install 4.x:

```bash
pip install "pyannote.audio==4.0.7"
```

### 2. Hold torch back, or you lose the GPU

`pyannote.audio` requires `torch>=2.0.0` (open-ended, not pinned). A plain
install can therefore resolve a stock CPU wheel over the ROCm build. Pin the
whole torch stack on the command line:

```bash
pip install "pyannote.audio==4.0.7" \
  "torch==2.11.0+rocm7.13.0" \
  "torchaudio==2.11.0+rocm7.13.0" \
  "torchvision==0.26.0+rocm7.13.0" \
  --extra-index-url https://download.pytorch.org/whl/rocm7.13
```

Then confirm the build survived:

```bash
textflowkit selftest
```

A ROCm install reports `torch <ver>+rocm*`; a stock CPU wheel reports plain
`torch <ver>`.

### 3. `torchcodec` cannot load on this torch - the code works around it

`pyannote.audio` 4.x decodes audio through `torchcodec`, whose bundled native
DLLs are built against specific torch releases and fail against ROCm torch:

```text
OSError: Could not load this library: ...libtorchcodec_core5.dll
```

`PyannoteDiarizer` does not rely on it. It reads audio with `soundfile` (already
a pyannote dependency) and hands pyannote an in-memory waveform dictionary -
which is the workaround pyannote's own error message names. You do not need a
working `torchcodec`.

### Access: three gated repos, not one

The model cards mention two. There are **three**, and the third is the one that
actually blocks a run:

| Repo | License | Needed for |
|---|---|---|
| `pyannote/speaker-diarization-3.1` | MIT (weights gated) | the pipeline definition |
| `pyannote/segmentation-3.0` | MIT (weights gated) | the segmentation stage |
| `pyannote/speaker-diarization-community-1` | **CC-BY-4.0** | the weights the pipeline downloads at runtime |

Accepting only the first two gets you a `403 GatedRepoError` on
`speaker-diarization-community-1` partway through loading. Each one needs
"Agree and access repository" clicked on its own page while logged in, then a
read token:

```bash
setx HF_TOKEN hf_xxxxxxxx
```

A **read** token is sufficient. Verify it reaches all three before blaming the
code:

```bash
python -c "from huggingface_hub import hf_hub_download as d; [d(r, 'README.md', token=__import__('os').environ['HF_TOKEN']) for r in ['pyannote/speaker-diarization-3.1','pyannote/segmentation-3.0','pyannote/speaker-diarization-community-1']]; print('all three OK')"
```

### Running it

```bash
textflowkit transcribe clip.wav --diarize --format json
```

Segments come back with a `speaker` field (`SPEAKER_00`, `SPEAKER_01`, ...) and
`metadata.diarization` records the backend, the speaker list, the turn count and
how many segments were labelled. If the backend or token is missing, the run
**fails with an actionable error** rather than returning empty speakers.

## CPU fallback

With no GPU, the engine selects CPU automatically. Pass `--device cpu` to force it.
CPU transcription is dramatically slower; prefer a smaller `--model`.
