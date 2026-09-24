Bundled PDF fonts (SIL Open Font License 1.1)
================================================

The fonts are embedded into generated PDFs by ReportLab. They are included in
the wheel so Unicode export does not depend on host-installed fonts.

* `NotoSans.ttf`: Noto Sans Regular, from
  https://github.com/notofonts/noto-fonts/tree/main/hinted/ttf/NotoSans
  SHA-256 `b85c38ecea8a7cfb39c24e395a4007474fa5a4fc864f6ee33309eb4948d232d5`
* `NotoSansArabic.ttf`: Noto Sans Arabic Regular, from
  https://github.com/notofonts/noto-fonts/tree/main/hinted/ttf/NotoSansArabic
  SHA-256 `ceea25b464a656dc3b26849bab9356740401af62aedf1bfa8b7f0d9b75925b1b`
* `NotoSansSC.ttf`: Noto Sans SC variable TrueType, from
  https://github.com/google/fonts/tree/main/ofl/notosanssc
  SHA-256 `a3041811a78c361b1de50f953c805e0244951c21c5bd412f7232ef0d899af0da`

`OFL-NotoSans.txt` covers the first two fonts. `OFL-NotoSansSC.txt` covers
Noto Sans SC. These font licenses are separate from textflowkit's Apache-2.0
license. Do not remove them from redistributed source or wheels.

Attribution reconciliation
--------------------------

`OFL-NotoSans.txt` is the upstream license file exactly as published. Its
copyright line reads:

    Copyright 2018 The Noto Project Authors (github.com/googlei18n/noto-fonts)

The two binaries that file covers, `NotoSans.ttf` and `NotoSansArabic.ttf`, also
carry an embedded `name`-table copyright record (nameID 0), and that record
names a different holder:

    Copyright 2015-2021 Google LLC. All Rights Reserved.

Both notices are retained exactly as published and both are listed here so a
redistributor sees the same two strings the license file and the binaries carry.
This note does not rank one notice above the other. It is an attribution
reconciliation between two published strings, not a legal opinion, and it does
not conclude that any license is missing or that either notice is wrong.

`NotoSansSC.ttf` is not covered by this note. It ships under its own license
file, `OFL-NotoSansSC.txt`, whose copyright line and whose binary's embedded
nameID 0 record both name Adobe; that notice is separate from the two above.
