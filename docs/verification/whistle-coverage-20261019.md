# Whistle coverage verification — 2026-10-19

Local verification of the Whistle missing-speech fixes shipped in core v0.1.13
(fonts unchanged at 0.1.6). Whistle remains the default engine; no model weights
or native executable pins changed.

Meeting source: Longmont City Council, September 8, 2026 —
<https://www.youtube.com/watch?v=3qfQAkAAC9U>. Duration 4:00:07
(14,407.0530625 s). Only derived word/timestamp counts and hashes are published
here; no recording media, transcript text, or private local paths are included.

## What changed

- Normal native inputs are limited to 15 seconds: 11-second owned cores with up
  to 2 seconds of context per side. The former 30-second plan reproducibly
  omitted passages that shorter inputs recovered.
- Short 4-11 s interior gaps get a bounded, differently framed native decode.
  Added words are accepted only between two matching timed anchors; original
  words and repetitions are retained, long pauses skipped, extra calls capped at
  two per core. This is native speech recognition, not LLM text filling.
- Shared-context word ownership is reconciled one-to-one, so real repetitions
  survive and ambiguous collapsed repetitions are left alone instead of being
  globally deduplicated.
- Words whose neighboring timestamps move slightly backwards are retained and
  repaired rather than deleted.
- Seam context is persisted in progress schema 2; schema-1 partial progress is
  refused rather than mixed with the new window plan, and completed transcripts
  are not rewritten.

## Measured results (one recording)

| Check | Result |
|---|---|
| Fresh native whole-meeting decode | 1,310 cores completed |
| Native decode elapsed | 1,113.909 s (18 min 34 s) |
| Sampled peak process-tree RSS | 102,125,568 bytes (97.4 MiB) |
| Final current-code transcript | 34,723 words |
| Invalid word bounds / backwards starts | 0 / 0 |
| Omission region 1:03:41.84-1:04:01.76 | 2 -> 50 words |
| Omission region 2:14:08.80-2:14:19.92 | 0 -> 47 words |
| Short-gap checks | 62 checks, 7 accepted, 53 words added |
| Resume after core 350 | Exact word text/start/end equality; 960 remaining cores requested |
| Resume after core 700 | Exact word text/start/end equality; 610 remaining cores requested |

## Proof tiers

1. The fresh whole-meeting native run (1,310 cores) was performed with the new
   15-second windows and gap checks, but its process had loaded the earlier
   seam=1 assembly policy before the final tolerance/timing-repair change
   (34,731 raw-run words before final assembly).
2. The final seam=2 public engine processed every actual saved native output
   from that run: it sliced and validated the real WAV windows, made the real
   native gap calls (62, each with a distinct gap WAV hash), and performed
   current parsing, ownership reconciliation, gap recovery, seam reconciliation,
   timing repair and progress handling. It reproduced the previously verified
   final transcript (34,723 words) and its receipt hash.
3. Resume checks at cores 350 and 700 replayed those native outputs, including
   the hash-bound gap responses, through the current public engine. They require
   exact text/timestamp equality and the exact unfinished-core sequence. They do
   not claim a second full native decode during resume.

## Automated gates

- Focused engine/seam suite: 113 passed, zero failures, errors or skips.
- Full suite: 2,097 passed, 18 skipped, 2 existing warnings. The 18 skips are not
  counted as passes.

## Limitations

- Both long missing passages are restored on this recording, and the remaining
  recognition/spelling errors are ordinary recognizer output; this is coverage
  evidence for one four-hour meeting, not human-scored WER and not a claim of
  100% accuracy or that every word of every recording is captured.
- Cross-recognizer and automatic-caption comparisons used for screening are not
  human ground truth; new candidates still require adjudication.
- This was a local verification run. No push, tag, publication, website
  deployment or installer rebuild is claimed here.
