# TextFlowKit user manual — v0.1.5

TextFlowKit turns a local audio/video file or a supported media URL into a
timestamped transcript. It is a **self-hosted developer tool**, not a hosted
transcription website. The same core is available through the CLI, Python,
MCP, and a JSON HTTP adapter. It runs on native Windows (no WSL), macOS, and
Linux. The software is Apache-2.0 and provided **as is, without warranty**.

## 1. Install and check the machine

Install Python 3.10 or later and `ffmpeg`/`ffprobe` on `PATH`. For a standard
CPU setup, install the current release from PyPI:

```bash
python -m pip install 'textflowkit[export,mcp,http]==0.1.5'
textflowkit --version
textflowkit doctor
textflowkit selftest
```

The `export` extra installs DOCX support and a separate `textflowkit-fonts`
package for offline multilingual PDFs. Omit extras you do not need. `doctor`
reports available tools, extras, and compute device. `selftest` runs a real
tiny-model transcription of bundled synthetic speech and checks for a known
word; it may need to download Whisper weights on first use.

**Existing Windows AMD ROCm installation:** do not use the generic install
command above if you already have a working ROCm PyTorch build. Normal pip
resolution can replace that build with CPU torch. Follow the
[native-Windows ROCm instructions](install.md#amd-gpu-support-rocm--no-wsl)
instead. The general [install notes](install.md) also cover JavaScript runtime
selection for yt-dlp, GPU checks, and optional diarization dependencies.

## 2. Transcribe one file or URL

```bash
textflowkit transcribe meeting.mp4 --model small --formats json,srt,txt --output-dir transcripts
textflowkit transcribe 'https://www.youtube.com/watch?v=EXAMPLE' --formats json,srt --output-dir transcripts
```

The default formats are JSON, SRT, and TXT. Other supported outputs are VTT,
Markdown, DOCX, and PDF. Output filenames include a job identifier so separate
runs do not silently overwrite one another. JSON is the full-fidelity format:
it retains segment timing, source text, optional speaker/translation fields,
and Whisper word timings. `duration` represents the decoded audio length,
including trailing silence, rather than the end of the last spoken segment.

To print only rendered text instead of writing files:

```bash
textflowkit transcribe meeting.mp4 --stdout --stdout-format txt
```

Platform links are acquired through yt-dlp and can stop working as sites
change their access rules. Of the 13 recognized platforms, only YouTube has a
maintained live URL release check; the others are not independently verified
on every release. See [sources and limitations](sources.md).

`--engine` chooses the speech engine. The default is `whisper`
(`openai-whisper` on the torch stack — ROCm on AMD, CUDA on NVIDIA, CPU
otherwise) and is unchanged. `--engine faster-whisper` is an opt-in CPU/Mac
engine; it needs `pip install "textflowkit[faster-whisper]"`, is not a ROCm
replacement, and is rejected on the command line with that install line before
fetching anything if the extra is missing. See
[install notes](install.md#optional-cpumac-engine-faster-whisper), including how
to keep an existing ROCm torch build.

Decoding runs under a wall-clock limit that applies in every mode, not only
production: `transcribe`, `batch`, the MCP server, and the HTTP adapter all
abort a decode that runs past it and report the setting to raise. The default
is 600 seconds. For a long recording, raise it before starting the process:

```powershell
$env:TEXTFLOWKIT_FFMPEG_TIMEOUT_SECONDS = '3600'
textflowkit transcribe long-meeting.mkv
```

```bash
export TEXTFLOWKIT_FFMPEG_TIMEOUT_SECONDS=3600
textflowkit transcribe long-meeting.mkv
```

The value is in seconds. See [adapter and production settings](adapters.md) for
the separate size and duration caps that only apply under the production
profile.

## 3. Batch and resume

Set a durable SQLite job store before relying on resume across process
restarts. In PowerShell:

```powershell
$env:TEXTFLOWKIT_DB = Join-Path $HOME 'textflowkit-jobs.db'
textflowkit batch meeting-a.mp4 meeting-b.mp4 --output-dir transcripts --resume
```

`--resume` reuses a matching checkpoint when the source and options still
match. Without `TEXTFLOWKIT_DB`, jobs are in memory and cannot survive a
process restart. The job executor bounds concurrent work; consult
[adapter and production settings](adapters.md) before running a service.

## 4. Export or inspect a saved transcript

```bash
textflowkit export transcript.json --format pdf --output transcript.pdf
textflowkit export transcript.json --format vtt --output transcript.vtt
```

PDF/DOCX requirements are checked **before** a new transcription job starts
when those formats are requested. If the optional `export` extra is missing,
the request fails with an installation hint rather than wasting a model run.
An existing JSON transcript can be re-rendered without retranscribing media.

SRT and WebVTT are wrapped for readability: a long segment becomes several
cues, no line is longer than about 42 characters, and no cue has more than two
lines. Cue times come from the source-language word timings when the segment
has them and they name the text being shown. A translation cannot use them -
its word timings describe the original speech - so its cue times are estimated
from the segment's own interval, and an imported transcript with no word
timings is estimated the same way. Wrapping is a readability change only: it
never rewrites, reorders, or drops text, and it does not realign words.

A speaker label repeats on every cue. In SRT the label is visible text, so it
counts toward that line's width and leaves the rest of the cue slightly
narrower; WebVTT carries the speaker as `<v ...>` markup, which is not visible
text and so is not counted. A label that is itself wider than the target, or
one containing line breaks, is kept whole rather than being shortened, which
can push a line past the target - text is never sacrificed to the width.

## 5. Python API

```python
from textflowkit import transcribe

result = transcribe("meeting.mp4", model="small", formats=["json", "srt"])
print(result.transcript.duration)
print(result.transcript.text)
for segment in result.transcript.segments:
    for word in segment.words:
        print(word.start, word.end, word.text)
```

`transcribe()` returns a `TranscribeResult` with `transcript` and `outputs`.
Pass `output_dir=` to write files, or omit it to keep only the Python result.
Saved JSON and Python results always retain source-language word timings.
If segment text was translated, its word timings still refer to the original
speech, **not** to individual translated words. Subtitle cue times follow the
same distinction: they come from the word timings for source text and are
estimated from the segment's interval otherwise.

## 6. MCP and HTTP integrations

An AI harness can launch `textflowkit-mcp` as a stdio process, or connect to
its Streamable HTTP endpoint. A software product can use `textflowkit-http`.
Both adapters submit jobs, return a job ID, and let clients poll or retrieve
results without holding a long request open.

```bash
textflowkit-mcp
textflowkit-http --host 127.0.0.1 --port 8767
```

The first command is for a harness to launch; run the HTTP command separately
when using its JSON API. Example PowerShell client:

```powershell
$base = 'http://127.0.0.1:8767'
$body = @{ source = 'C:\media\meeting.mp4'; model = 'small' } | ConvertTo-Json
$job = Invoke-RestMethod -Method Post -Uri "$base/jobs" -ContentType 'application/json' -Body $body
Invoke-RestMethod "$base/jobs/$($job.id)"  # poll until state is done
Invoke-RestMethod "$base/jobs/$($job.id)/transcript?include_words=true"
```

HTTP and MCP **JSON reads omit word timings by default** to keep responses
small. Set `include_words=true` to receive them. This changes only the read
response: saved JSON, SQLite job records, and resume checkpoints still hold
the words. For long transcripts, use `offset`/`limit`, `start`/`end`, or search
rather than returning everything at once. See the complete
[adapter guide](adapters.md) for MCP harness configuration, all routes/tools,
batching, cancellation, paging, and production settings.

The HTTP server is local-only by default. Do not expose it on a network
without the documented authentication/TLS gateway, input/output boundaries,
and SSRF-filtering egress proxy. Long model calls cancel cooperatively at
their next stage boundary, not immediately. MCP and HTTP jobs decode under the
same `TEXTFLOWKIT_FFMPEG_TIMEOUT_SECONDS` wall-clock limit as the CLI
(see [transcribe one file or URL](#2-transcribe-one-file-or-url)).

## 7. Release and help

- [v0.1.5 GitHub release](https://github.com/scottconverse/textflowkit/releases/tag/v0.1.5)
- [Core package on PyPI](https://pypi.org/project/textflowkit/0.1.5/) and
  [optional font package](https://pypi.org/project/textflowkit-fonts/0.1.5/)
- [Release verification procedure](release-checklist.md),
  [security policy](../SECURITY.md), and [issues](https://github.com/scottconverse/textflowkit/issues)

The v0.1.5 release passed Windows/Linux/macOS CI, a local native-Windows
YouTube smoke on the release commit, and a clean PyPI install with real
transcription and PDF output. Those checks do not establish live access to
every supported website, every AI harness, or every GPU configuration.

**PyPI description erratum:** the immutable v0.1.5 long description was built
from an earlier README and still says “v0.1.4 release” in its status paragraph.
The uploaded wheel and source archive are version 0.1.5; their hashes match
the GitHub v0.1.5 release. The current GitHub README and this manual correct
the wording. PyPI's already-uploaded release metadata cannot be rewritten in
place; a future package version will carry the corrected description.
