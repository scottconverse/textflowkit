# Speech self-test fixture

`selftest-speech.wav` is a short synthetic voice clip generated for this project
with Windows `System.Speech.Synthesis.SpeechSynthesizer`, saying: "Text flow kit
can transcribe spoken words." It contains no person’s voice or private media.
The fixture is deliberately bundled in the core wheel so `textflowkit selftest`
can verify that the installed model produces at least one nonempty timed speech
segment, not merely that it can run on silence. It is not an accuracy benchmark.
