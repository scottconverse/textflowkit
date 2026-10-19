"""Run in a separate environment: pip install fermion-research==0.2.11"""
from pathlib import Path
import os, json, wave, dataclasses
os.environ["FERMION_DEVICE"] = "cpu"
os.environ["FERMION_CPU_THREADS"] = "16"
import numpy as np
import fermion

root = Path(__file__).resolve().parent
model = fermion.load_speech("phonon-2")
for index in (2, 361, 441):
    with wave.open(str(root / f"diagnostic-chunk-{index:04d}.wav")) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32) / 32768
    result = model.transcribe_array_detailed(audio)
    print(json.dumps({"index": index, "result": dataclasses.asdict(result)}, ensure_ascii=False), flush=True)
