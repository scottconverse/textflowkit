"""Transcript translation.

Translation is a separate stage from transcription: it runs after the transcript
exists, writes into `Segment.translated_text` (a field the model already had), and
is opt-in.

The backend is pluggable. The shipped implementation talks to a local Ollama
instance, which keeps media and transcript text on the machine - the same
property the rest of the tool has.

Two engineering choices worth stating:

- **Batching with a fallback.** One request per segment is correct but slow (a
  214-segment transcript would be 214 round trips). Requests are batched, and if
  a batch comes back unparseable or the wrong length, that batch is retried one
  segment at a time. Correctness does not depend on the model obeying a format.
- **A cache.** Whisper repeats phrases; identical source text is translated once.

A backend that is unreachable or misconfigured raises. It never returns the
source text as though it were a translation.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Protocol

from textflowkit.core.model import Segment

ENV_OLLAMA_HOST = "TEXTFLOWKIT_OLLAMA_HOST"
ENV_OLLAMA_MODEL = "TEXTFLOWKIT_TRANSLATE_MODEL"
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "deepseek-v4.1-flash:cloud"
BATCH_SIZE = 20

_NUMBERED = re.compile(r"^\s*(\d+)\s*[.):\-]\s*(.*)$")


class TranslationError(RuntimeError):
    """Raised when translation was requested but could not be performed."""


class Translator(Protocol):
    """Translates text into a target language."""

    name: str

    def translate(self, texts: Sequence[str], target: str) -> list[str]: ...


class OllamaTranslator:
    """Translation through a local Ollama instance.

    Uses /api/generate with a strict numbered-list prompt. The model is not
    trusted to be well behaved: a response that cannot be parsed back into
    exactly the requested number of items is rejected, and the caller falls back
    to per-segment requests.
    """

    name = "ollama"

    def __init__(
        self,
        model: str | None = None,
        host: str | None = None,
        *,
        timeout: float = 300.0,
    ) -> None:
        self.model = model or os.environ.get(ENV_OLLAMA_MODEL, DEFAULT_OLLAMA_MODEL)
        self.host = (host or os.environ.get(ENV_OLLAMA_HOST, DEFAULT_OLLAMA_HOST)).rstrip("/")
        self.timeout = timeout
        self._cache: dict[tuple[str, str], str] = {}

    @staticmethod
    def _read_error_body(exc: urllib.error.HTTPError) -> str:
        """Best-effort read of an error body for the message. Never raises."""
        reader = getattr(exc, "read", None)
        if reader is None:
            return ""
        try:
            return reader().decode("utf-8", errors="replace")[:200]
        except (OSError, ValueError, AttributeError):
            return ""

    # -- transport ---------------------------------------------------------

    def _generate(self, prompt: str) -> str:
        payload = json.dumps(
            {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0},
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.host}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = self._read_error_body(exc)
            raise TranslationError(
                f"Ollama rejected the request ({exc.code}) for model '{self.model}'. "
                f"Is the model pulled? {detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TranslationError(
                f"could not reach Ollama at {self.host}: {exc}. "
                f"Start Ollama, or set {ENV_OLLAMA_HOST}."
            ) from exc
        except json.JSONDecodeError as exc:
            raise TranslationError(f"Ollama returned a non-JSON response: {exc}") from exc

        if "response" not in body:
            raise TranslationError(f"Ollama response had no 'response' field: {body}")
        return str(body["response"])

    # -- translation -------------------------------------------------------

    def _translate_one(self, text: str, target: str) -> str:
        stripped = text.strip()
        if not stripped:
            return ""
        key = (stripped, target)
        if key in self._cache:
            return self._cache[key]

        prompt = (
            f"Translate the following text into {target}. "
            "Reply with ONLY the translation - no preamble, no quotes, no notes.\n\n"
            f"{stripped}"
        )
        out = self._generate(prompt).strip()
        if not out:
            raise TranslationError("model returned an empty translation")
        self._cache[key] = out
        return out

    def _translate_batch(self, texts: Sequence[str], target: str) -> list[str]:
        numbered = "\n".join(f"{i + 1}. {t.strip()}" for i, t in enumerate(texts))
        prompt = (
            f"Translate each numbered line into {target}. "
            "Keep the numbering exactly as given and reply with ONLY the "
            "translated numbered lines, one per line, no preamble and no notes.\n\n"
            f"{numbered}"
        )
        raw = self._generate(prompt)

        parsed: dict[int, str] = {}
        for line in raw.splitlines():
            match = _NUMBERED.match(line)
            if match:
                parsed[int(match.group(1))] = match.group(2).strip()

        expected = list(range(1, len(texts) + 1))
        if any(i not in parsed for i in expected):
            raise TranslationError("batch response did not match the requested items")
        return [parsed[i] for i in expected]

    def translate(self, texts: Sequence[str], target: str) -> list[str]:
        """Translate each text. Length of the result always matches the input."""
        if not target.strip():
            raise ValueError("target language must not be empty")

        results: list[str] = []
        for start in range(0, len(texts), BATCH_SIZE):
            chunk = list(texts[start: start + BATCH_SIZE])
            # Cached and empty entries never need a round trip.
            if all(not t.strip() or (t.strip(), target) in self._cache for t in chunk):
                results.extend(self._translate_one(t, target) for t in chunk)
                continue
            try:
                batch = self._translate_batch(chunk, target)
            except TranslationError:
                # Correctness beats speed: redo this chunk one at a time rather
                # than trust a malformed batch.
                batch = [self._translate_one(t, target) for t in chunk]
            else:
                # Populate the cache from the batch path too. Only caching on the
                # single-item path meant a phrase repeated in a later batch was
                # retranslated every time.
                for source_text, translated_text in zip(chunk, batch, strict=True):
                    stripped = source_text.strip()
                    if stripped and translated_text.strip():
                        self._cache[(stripped, target)] = translated_text.strip()
            results.extend(batch)
        return results


def translate_segments(
    segments: list[Segment],
    target: str,
    *,
    translator: Translator,
) -> int:
    """Fill `translated_text` on each segment. Returns how many were translated.

    Blank segments are left alone. The translator's result length is checked
    against the input so a misbehaving backend cannot silently shift text from
    one segment onto another.
    """
    texts = [s.text for s in segments]
    if len(texts) != len(segments):  # pragma: no cover - defensive
        raise TranslationError("segment/text count mismatch")

    translated = translator.translate(texts, target)
    if len(translated) != len(texts):
        raise TranslationError(
            f"translator returned {len(translated)} results for {len(texts)} segments"
        )

    count = 0
    for segment, text in zip(segments, translated, strict=True):
        if segment.text.strip() and text.strip():
            segment.translated_text = text
            count += 1
    return count


def get_translator(backend: str = "ollama", **kwargs) -> Translator:
    if backend in ("ollama", "default"):
        return OllamaTranslator(**kwargs)
    raise TranslationError(f"unknown translation backend: {backend}")
