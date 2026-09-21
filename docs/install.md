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

## CPU fallback

With no GPU, the engine selects CPU automatically. Pass `--device cpu` to force it.
CPU transcription is dramatically slower; prefer a smaller `--model`.

