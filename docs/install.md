# Install notes

Current release: [v0.1.10](https://github.com/scottconverse/textflowkit/releases/tag/v0.1.10).
Use `textflowkit --version` to confirm the installed version. For everyday
commands and outputs, start with the [user manual](user-manual.md).

As of v0.1.9 the default engine is **Whistle**, a CPU-only native CLI that needs
no PyTorch; `openai-whisper` is moved to the optional `whisper` extra and is
selected with `--engine whisper`. The ROCm instructions below apply to the
explicit `whisper` engine and to diarization — **Whistle itself never needs ROCm
or WSL**.

## Local browser interface

The local browser workspace **ships in the core package as of v0.1.10**. A
regular install of the current release carries the `textflowkit-ui` command:

```bash
python -m pip install 'textflowkit[http,export]==0.1.10'
textflowkit-ui
```

It adds **no new dependency of its own**: the HTTP extra already supplies FastAPI
and uvicorn, so an `http` install is what you need. The normal way to open it on
Windows is the desktop shortcut:

```powershell
textflowkit-ui --create-shortcut                 # Start Menu entry (pythonw, no console)
textflowkit-ui --create-shortcut --shortcut-dir 'C:\path\to\folder'
```

It uses Windows PowerShell and needs no `pywin32`, so an HTTP-only install can
create one. The shortcut is created only on request. Under `pythonw` there is no
console, so a startup failure is written to a log file under the per-user data
directory and shown in a message box rather than failing silently.

The UI is served on **loopback only** and is never reachable off the machine: it
binds `127.0.0.1` and does **not** honour `TEXTFLOWKIT_ALLOW_REMOTE`. It is a
**local app, not a public website** — the [landing page](https://www.textflowkit.org/)
is static documentation that does not run the pipeline. **Stop server** in the
workspace drains — it waits for current jobs to finish rather than interrupting
them; cancel a job first to stop it early. See the
[user manual](user-manual.md#7-local-browser-interface) and the
[developer manual](adapters.md#local-browser-interface-textflowkit-ui).

## Requirements

- **Python ≥ 3.10**
- **ffmpeg** on `PATH` (ffprobe comes with it)
- **yt-dlp** — installed as a dependency

For a standard CPU or NVIDIA environment, install from
[PyPI](https://pypi.org/project/textflowkit/) with
`python -m pip install textflowkit`. The default engine (Whistle)
pulls no torch; to use the `openai-whisper` torch engine, install the `whisper`
extra (`python -m pip install 'textflowkit[whisper]'`) and select it with
`--engine whisper`. For an existing AMD ROCm environment, follow the
instructions below instead of allowing pip to replace your torch.

## Default engine: Whistle

A plain `python -m pip install textflowkit` gets Whistle:

- **No torch, no extra.** Whistle is a CPU-only native CLI. It downloads a pinned
  native binary and one pinned model (about 17 MB) on first use, outside the
  package and the wheel. Nothing else is required.
- **Platforms:** Windows x86-64/arm64, Linux x86-64/arm64, and Apple Silicon.
  **Intel Macs are not supported** — the refusal names the explicit `whisper`
  engine, which you can install with `pip install 'textflowkit[whisper]'`. No WSL
  is involved.
- **Languages:** `en`, `de`, `fr`, `es`, `it`, `nl`, `pl`. Any other language, or a
  non-CPU device, is refused before anything is fetched. Whistle is never swapped
  for another engine to satisfy a request.
- **Offline and cache.** Set `TEXTFLOWKIT_OFFLINE` to refuse any download, and
  `TEXTFLOWKIT_MODELS_DIR` to choose the asset directory. The Whistle asset helper
  downloads only pinned runtime/model assets; media acquisition and optional
  translation may independently use the network. These downloads are not telemetry.
- **Telemetry is off, always.** Every Whistle child forces `NEEDLE_TELEMETRY=0`,
  `DO_NOT_TRACK=1`, and `CI=1`, overriding a parent that opted in; there is no
  opt-in setting. This applies the upstream gate — it is not a claim that the
  upstream binary's tracking code is physically removed.
- **ROCm is not needed.** The instruction above applies to the explicit `whisper`
  engine and to diarization, never to Whistle.

`doctor` reports the default engine, the Whistle platform and cache, whether the
model is cached, offline mode, and the telemetry gate, all with no network. For
the full engine behaviour — windowing, resume, cancellation — see the
[user manual](user-manual.md#7-whistle-the-default-engine).

## AMD GPU support (ROCm) — no WSL required

**This section is for the explicit `openai-whisper` engine and for diarization.**
The default Whistle engine is CPU-only and needs neither ROCm nor WSL, so if you
are using the default engine you can skip this section entirely.

textflowkit runs natively on Windows. On AMD hardware, GPU acceleration for
`openai-whisper` comes from a **ROCm build of PyTorch**; no Linux layer or WSL is
involved.

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
disabling GPU acceleration. (Whistle, the default engine, has no
torch dependency and is unaffected.)

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

`selftest` defaults to Whistle (CPU-only, no torch): it normalizes
the bundled synthetic speech fixture to the engine's sample rate and runs a real
transcription. It fails if the engine returns no nonempty, timed speech segment. A
PASS proves text generation, not transcription accuracy. `selftest --engine
whisper --model tiny` checks the openai-whisper/torch stack instead, running a real
matmul on the selected device and then a real Whisper pass; that path prints
PASS/FAIL for each stage and names the torch build, so a ROCm install reports
`torch <ver>+rocm*` and the device name, while a stock CPU wheel reports plain
`torch <ver>`.

`--skip-transcribe` is cheap enough to run in CI and is exercised there on every
platform.

## Diarization

Diarization is opt-in and needs three separate things, all verified on this
machine (Windows, ROCm torch 2.11.0+rocm7.13.0, `pyannote.audio` 4.0.7). That
stack is historical compatibility evidence, not an instruction to replace a
working torch install.

### 1. Choose the install path **before** running pip

`pyannote.audio` requires `torch>=2.0.0` (open-ended, not pinned), and installing
the `textflowkit[diarize]` extra resolves the **whole project** - including the
`whisper` extra's `openai-whisper` if you also ask for it. Either can therefore
resolve a stock CPU wheel over a ROCm build, silently losing the GPU. Decide
which case you are in first:

**If a native Windows AMD ROCm stack already works, do not run a plain pyannote
or extra install first.** Inspect the installed stack, then let pip resolve only
the pyannote version while preserving the torch, torchaudio, and torchvision
builds you already have:

```powershell
python -c "import torch, torchaudio, torchvision; print(torch.__version__); print(torchaudio.__version__); print(torchvision.__version__); print(torch.version.hip, torch.cuda.is_available())"
python -m pip check
```

Use the reported versions in a constraints file and your existing verified AMD
package source. The one-line example below corresponds to the historical
reference versions; use it only if those builds are already installed or
available from your chosen source:

```powershell
python -m pip install "pyannote.audio==4.0.7" "torch==2.11.0+rocm7.13.0" "torchaudio==2.11.0+rocm7.13.0" "torchvision==0.26.0+rocm7.13.0"
```

**If you have no ROCm stack to preserve** (a fresh CPU environment), the plain
extra install is the safe path:

```powershell
python -m pip install "textflowkit[diarize]"
```

The project declares `pyannote.audio>=4.0`, so this resolves a current compatible
version rather than promising the historical 4.0.7. The reason pyannote must be
4.x: 3.4.x calls `torchaudio.AudioMetaData`, which no longer exists in
torchaudio 2.11 (importing 3.4.0 raises `AttributeError: module 'torchaudio' has
no attribute 'AudioMetaData'`); 4.x removed that dependency.

After either install, confirm the build survived and repeat the inspection and
`python -m pip check`:

```bash
textflowkit selftest
```

A ROCm install reports `torch <ver>+rocm*`; a stock CPU wheel reports plain
`torch <ver>`. An unchanged version string alone is not proof that inference
works. If pip cannot satisfy the pinned stack, stop and reconcile that
environment's package sources and requirements rather than adding an unverified
index or substituting stock torch.

### 2. `torchcodec` cannot load on this torch - the code works around it

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
read token.

Set the token **in the same shell you will run the command from**, before running
it - the process reads the environment when it starts, so a token set after the
command, or set in another window, never reaches that run.

Native Windows PowerShell:

```powershell
$env:HF_TOKEN = 'hf_xxxxxxxx'
```

Linux, macOS, WSL, or Git Bash:

```bash
export HF_TOKEN='hf_xxxxxxxx'
```

A **read** token is sufficient. Verify it reaches all three before blaming the
code - run this in the same shell where you set the token (the one-liner is
identical in PowerShell):

```bash
python -c "from huggingface_hub import hf_hub_download as d; [d(r, 'README.md', token=__import__('os').environ['HF_TOKEN']) for r in ['pyannote/speaker-diarization-3.1','pyannote/segmentation-3.0','pyannote/speaker-diarization-community-1']]; print('all three OK')"
```

### Running it

The session-scoped assignment above still applies to this command:

```bash
textflowkit transcribe clip.wav --diarize --formats json
```

`setx HF_TOKEN "hf_xxxxxxxx"` is a separate, optional step for **new shells**
only: it writes the value for shells started afterwards and does **not** affect
the window you are in, so it can never replace the session-scoped assignment
above.

Segments come back with a `speaker` field (`SPEAKER_00`, `SPEAKER_01`, ...) and
`metadata.diarization` records the backend, the speaker list, the turn count and
how many segments were labelled. If the backend or token is missing, the run
**fails with an actionable error** rather than returning empty speakers.

## CPU fallback

With no GPU, the Whisper-family engines select CPU automatically; pass
`--device cpu` to force it, and prefer a smaller `--model` since CPU Whisper is
dramatically slower. The default Whistle engine is CPU-only already, so
`--device` does not apply to it — naming a non-CPU device for Whistle is refused,
and the message points at `--engine whisper` for the GPU path.

## Optional CPU/Mac engine: faster-whisper

`openai-whisper` on CPU is slow, which hurts most on CPU-only machines and Apple
Silicon. `faster-whisper` (CTranslate2) is an **opt-in** alternative engine:

```bash
python -m pip install "textflowkit[faster-whisper]"
textflowkit transcribe meeting.mp4 --engine faster-whisper
```

The double quotes matter: CMD treats single quotes as literal characters, so a
single-quoted extra name is passed to pip with the quotes still on it.

- It is an extra, never a base dependency. The default engine is
  Whistle, so `faster-whisper` is reached only by naming it; without `--engine`
  you get Whistle (CPU, no torch).
- With no `--device`, or `--device cpu`, it runs CPU `int8`.
- It is **not** the ROCm path. CTranslate2's GPU path is CUDA-only (a ROCm build
  means compiling it yourself with `-DWITH_HIP=ON`), so on AMD Windows use the
  `whisper` engine (`--engine whisper`) for GPU. Note `--engine faster-whisper
  --device cuda` passes `cuda` straight to upstream; on an AMD box that fails
  there, and that failure is the point - the device is never silently rewritten
  into a CPU run.
- Its decoding defaults differ from `openai-whisper`, so the same audio can
  produce different text. No speed or accuracy comparison is claimed here;
  measure on your own machine.

**If you already have a ROCm torch build,** do not let this install re-resolve
your environment. CTranslate2 itself has no torch dependency, but `pip install`
resolves the *whole project*, including the `whisper` extra's `openai-whisper` if
you also ask for it, and that resolution is what disturbs a working ROCm setup.
Install the way the ROCm section above does - without allowing dependency
resolution:

```bash
python -m pip install "textflowkit[faster-whisper]" --no-deps
python -m pip install faster-whisper
```

Then re-check `python -c "import torch; print(torch.__version__, torch.version.hip)"`.
A faster-whisper install alongside a ROCm torch build has **not** been measured
here, so treat it as unverified on your machine rather than as a supported
combination.

**When the extra is missing:** it is checked on every surface before any media is
fetched, so you get the install line above instead of a download followed by a
traceback. The command line and the Python API (`transcribe(engine="faster-whisper")`)
both refuse up front, and so do the MCP and HTTP adapters - an unknown engine name
or a missing extra is reported there as `{"error": ...}` or HTTP 422 before a job
record is written. A saved request is checked the same way when it is resumed; a
job that already finished is the exception - it is answered from its stored
transcript and needs no engine.
