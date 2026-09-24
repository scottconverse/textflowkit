"""Shared cue-text safety for the time-coded subtitle renderers.

SRT and WebVTT agree on two structural rules: a blank line ends a cue, and a
line containing ``-->`` is a timing line. A segment's own text can violate
both, so it is not trusted here - a translation is model output and an
imported transcript is whatever its source held - and both renderers run the
payload through the helpers below.

The formats then diverge, and the helpers keep that difference explicit.
WebVTT cue text is *parsed*: ``&`` and ``<`` are markup syntax, so reserved
characters become character references and the payload's own ``>`` becomes
``&gt;``, which is also what removes a literal ``-->``. WebVTT's ``<v ...>``
voice span is intentional markup and is preserved, but its annotation is
sanitized because the tag ends at the first ``>``. SRT has no escape
mechanism at all - a player shows ``&lt;`` to the viewer rather than decoding
it - so SRT text stays literal and only the structural hazards are removed.
"""

from __future__ import annotations

# A cue payload must not contain this substring; WebVTT states the rule
# outright, and an SRT parser reads any such line as a timing line.
ARROW = "-->"

# SRT cannot escape a literal arrow, so it gets a readable stand-in that
# carries the same meaning without the timing syntax. It is deliberately plain
# ASCII: SRT is written as UTF-8 but is read by consumers as anything at all,
# and this keeps an ASCII transcript's cues ASCII.
ARROW_STAND_IN = "->"


def normalize_cue_lines(text: str) -> str:
    """Reduce a payload to non-empty lines joined by single line breaks.

    An interior blank line would otherwise end the cue and strand the text
    after it; surrounding whitespace is trimmed for the same reason.
    """
    lines = (line.strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def neutralize_arrow(text: str) -> str:
    """Replace a literal "-->" that a parser would read as a timing line.

    Repeats until no occurrence is left: the replacement is shorter than what
    it replaces, so a run such as "---->" would otherwise still hold "-->" at
    the end of the same pass. The loop terminates because each pass shortens
    the payload.
    """
    while ARROW in text:
        text = text.replace(ARROW, ARROW_STAND_IN)
    return text


def escape_vtt_text(text: str) -> str:
    """Escape WebVTT cue text, where "&" and "<" are syntax rather than text.

    Escaping ">" as well removes any literal "-->" the payload still holds,
    and a compliant player renders all three references back as themselves.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_vtt_annotation(text: str) -> str:
    """Sanitize the annotation of a ``<v ...>`` voice span.

    The annotation ends at the first ">", cannot span lines, and is entity
    parsed, so newlines collapse to spaces and "&" and ">" cannot stand raw.
    Returns an empty string when nothing but whitespace was given, which the
    caller reads as "no voice span" - ``<v >`` has no annotation and is not a
    valid start tag.
    """
    collapsed = " ".join(text.split())
    return collapsed.replace("&", "&amp;").replace(">", "&gt;")
