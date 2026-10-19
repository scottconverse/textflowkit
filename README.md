# Phonon-2 CPU speech-omission reproduction (2026-10-19)

Three 30-second excerpts from the public Longmont City Council meeting of September 8, 2026:
https://www.youtube.com/watch?v=3qfQAkAAC9U

| WAV | Source interval | Omitted content |
|---|---|---|
| diagnostic-chunk-0002.wav | 00:00:50–00:01:20 | Public-comment instructions following the pledge |
| diagnostic-chunk-0361.wav | 02:36:24–02:36:54 | RFP deadline/amendment discussion |
| diagnostic-chunk-0441.wav | 03:11:04–03:11:34 | Insurance premium question |

These are 16 kHz mono PCM16 WAVs extracted from a normalized meeting recording, without enhancement. SHA256SUMS pins the exact inputs. No model weights are included. The clips are third-party public meeting material supplied for reproducing this issue, not a claim of ownership or an Apache license for the recording.

## Reproduce

In a separate Python environment, install `fermion-research==0.2.11`, then run `python reproduce.py`. The package downloads Phonon-2 on first use. No TextFlowKit dependency, overlap trimming, speech filtering, or cleanup is involved. Use a fresh process with no FERMION_P2_CPU* overrides for the native baseline.

Tested: native Windows 11 x64 build 26300, Python 3.13.5, Ryzen AI MAX+395, CPU/16 threads, AVX512-VNNI, C packed-int8 encoder and C TDT decoder. Exact installed versions are in tested-environment.txt (including torch 2.14.1, transformers 5.20.1, numpy 2.5.4). These tests do not establish behavior on other platforms.

Model: FermionResearch/Phonon-2, model_phonon2_c4c_int6, downloaded revision 7e153e4054c0a10db6d47d6fff5c062d4c77c154. Inspected upstream repo revision: cf97fdbedaf5bfd3cc8b88c946e64c0fdc2587b1. The runnable reproducer uses the package's normal model resolution; compare model checksums if upstream weights change.

Actual baseline text (also in native-results.json):
- 0002: "One nation under God, indivisible, with liberty and justice for all."
- 0361: "But then when the nineteenth half"
- 0441: "Yeah. We're still in the process."

All return truncated=false. Expected: recognition covering the speech throughout each clip, not only these fragments. There is no hand-verified reference transcript or WER claim.

## Diagnostic experiments

`python diagnostic_variants.py MODE` reproduces the diagnostic operations; MODE can be torch-decoder, torch-encoder, fp32, true-frames, or candidate. It writes MODE.json in this folder, replacing the supplied reference result; copy the folder first if retaining it. Frontend variants replace a private method only in that process, not the installed package. They are hypotheses, not recommended patches.

| Mode | 0002 words | 0361 words | 0441 words |
|---|---:|---:|---:|
| Native baseline | 11 | 6 | 6 |
| Native encoder / Torch decoder | 67 | 6 | 6 |
| Torch encoder graph / packed kernels / C decoder | 11 | 6 | 6 |
| Dense fp32 encoder / Torch decoder | 11 | 6 | 97 |
| Valid-frame frontend / native decoder | 11 | 78 | 97 |
| Valid-frame frontend / Torch decoder | 67 | 6 | 97 |

Counts describe coverage differences, not accuracy scores. Full outputs are provided for comparison. Dense fp32 still uses reconstructed compressed Phonon weights, not the original teacher. No tested setting fixes all three clips.

The stock frontend normalizes 3001 spectrogram frames for these clips; the installed Parakeet feature extractor counts 3000 valid frames and excludes padding from normalization. The experiment uses floor(samples/160) frames for normalization and the native encoder. Its partial recovery does not establish this as the sole cause.

decoder-trace-0361.json records the reference Torch decoder with native encoder: finite features/encoder/logits; 147 steps, 137 blanks, 10 nonblank token predictions; first nonblank at 28.64 seconds. This localizes omission to recognition decisions rather than downstream text assembly. It does not identify the underlying defect.

Separately, our experimental overlapping-chunk assembly duplicated 17 words because independent timestamps straddled core boundaries. Context alignment corrected those, preserving two genuine-looking repeated-word candidates. That wrapper problem is ours; the raw single-clip omissions above occur before it.
