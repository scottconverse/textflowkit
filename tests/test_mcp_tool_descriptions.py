"""The MCP tool descriptions are documentation the *agent* consumes, not the reader.

A harness never reads this repository. It calls `tools/list` and gets back each
tool's name, its JSON schema, and the docstring that becomes its description. So
a description that lists fewer output formats than the tool's own validator
accepts - or that promises an ordering the retrieval core does not produce - is a
defect on the one surface an agent actually sees.

These tests read the descriptions off the tool objects the server publishes, not
out of the source file, so a sentence that never reaches `tools/list` cannot
satisfy them. Format names are compared against the same constants the validators
use (`SUPPORTED_FORMATS` / `TEXT_FORMATS`), so the test measures agreement between
prose and code rather than matching a frozen string.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp", reason="the mcp extra is required for the MCP adapter")

from textflowkit.adapters.mcp_server import mcp
from textflowkit.core.jobs import JobState
from textflowkit.core.model import Segment, Transcript
from textflowkit.core.retrieval import search_segments
from textflowkit.render import BINARY_FORMATS, SUPPORTED_FORMATS, TEXT_FORMATS

TOOLS = mcp._tool_manager._tools

# CPython 3.13 started stripping the source indentation out of a compiled
# docstring; 3.10-3.12 keep it. The MCP SDK publishes `fn.__doc__` verbatim -
# `Tool.from_function` does `description or fn.__doc__ or ""`, with no dedent of
# its own - so the description a pre-3.13 interpreter serves from `tools/list`
# carries the function body's own four spaces on top of the `Args:` layout:
# eight before a parameter name, not four. These fixtures are that shape, and
# exist so the indent assumption can be exercised without a second interpreter.
PRE_313_TRANSCRIBE_DESCRIPTION = (
    "\n"
    "    Start transcribing a media URL or local file. Returns immediately with a job id.\n"
    "\n"
    "    Args:\n"
    "        source: A media URL or a path to a local file.\n"
    "        formats: Comma-separated outputs to write when output_dir is set.\n"
    "            Available: txt, srt, vtt, md, json, docx, pdf. The binary formats\n"
    "            (docx, pdf) are written to disk and require the export extra.\n"
    "        output_dir: Directory to write rendered files into.\n"
    "    "
)

UNINDENTED_ARGS_DESCRIPTION = (
    "Args:\n"
    "source: A media URL or a path to a local file.\n"
    "formats: Comma-separated outputs. Available: txt, docx.\n"
)


# A line that starts an `Args:` entry: optional indent, a parameter name, the
# colon. The indent is captured only to rank lines against each other.
_ARG_LINE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<name>[A-Za-z_][A-Za-z0-9_]*):(?P<body>.*)$", re.MULTILINE
)


def _args_section(description: str) -> str | None:
    """Everything after the `Args:` heading, or None if there is none.

    The heading is normally a line of its own, which is the shape to prefer; a
    description that mentions `Args:` inside another line still has a section,
    so fall back to the plain search this parser has always used rather than
    refusing a description the old code would have read.
    """
    header = re.search(r"(?m)^[ \t]*Args:[ \t]*$", description)
    if header is not None:
        return description[header.end() :]
    index = description.find("Args:")
    return None if index < 0 else description[index + len("Args:") :]


def _entries(section: str) -> list[re.Match[str]]:
    """The lines that start an `Args:` entry.

    The first named line sets the indentation for the block, and a named line at
    any other depth is body text - a wrapped continuation of the entry above it,
    or prose such as `Available: ...`. So an unrelated sentence with a colon
    cannot pass for an argument name, and an outdented one cannot demote the
    entries that follow it into somebody's body.
    """
    candidates = list(_ARG_LINE.finditer(section))
    if not candidates:
        return []
    indent = candidates[0].group("indent")
    return [m for m in candidates if m.group("indent") == indent]


def arg_names_in(description: str) -> list[str]:
    """The argument names the description's `Args:` section declares."""
    section = _args_section(description)
    return [] if section is None else [m.group("name") for m in _entries(section)]


def arg_help_in(description: str, arg: str, label: str = "description") -> str:
    """The whitespace-normalized `Args:` entry for `arg` in `description`.

    Entries are located by argument name and line boundary, never by a fixed
    indent: the width of that indent is an artifact of the interpreter that
    compiled the docstring, not part of what the description says.
    """
    section = _args_section(description)
    assert section is not None, f"{label} has no Args section"
    entries = _entries(section)
    for i, entry in enumerate(entries):
        if entry.group("name") != arg:
            continue
        stop = entries[i + 1].start() if i + 1 < len(entries) else len(section)
        body = section[entry.end("name") + 1 : stop]
        return " ".join(body.split())
    raise AssertionError(f"{label} has no Args entry for {arg!r}")


def arg_help(tool: str, arg: str) -> str:
    """The same, read off the description the tool object actually publishes."""
    return arg_help_in(TOOLS[tool].description, arg, label=tool)


def formats_named(text: str) -> set[str]:
    """Which known output formats this prose names."""
    return {fmt for fmt in SUPPORTED_FORMATS if re.search(rf"\b{fmt}\b", text)}


def test_arg_help_reads_the_published_descriptions():
    """Guard: if the docstring layout changes, fail here rather than pass vacuously."""
    assert arg_help("transcribe_media", "formats").startswith("Comma-separated")
    assert arg_help("get_transcript", "fmt").startswith("How to render")


def test_arg_help_reads_a_pre_313_style_description():
    """The shape CI's 3.10-3.12 interpreters publish must parse like 3.13's."""
    help_text = arg_help_in(PRE_313_TRANSCRIBE_DESCRIPTION, "formats")
    assert help_text.startswith("Comma-separated")
    # the wrapped continuation lines belong to the entry above them
    assert formats_named(help_text) == set(SUPPORTED_FORMATS)


def test_arg_help_reads_an_unindented_args_section():
    """Indentation at all is a layout choice, not part of the description."""
    assert arg_help_in(UNINDENTED_ARGS_DESCRIPTION, "formats") == (
        "Comma-separated outputs. Available: txt, docx."
    )


def test_arg_help_ignores_a_colon_bearing_continuation_line():
    """`Available: ...` inside an entry's body is prose, not an argument name."""
    with pytest.raises(AssertionError):
        arg_help_in(PRE_313_TRANSCRIBE_DESCRIPTION, "Available")


def test_arg_help_keeps_the_entry_indent_the_first_named_line_set():
    """An outdented named line cannot demote the entries after it into a body."""
    description = (
        "Args:\n"
        "    source: A media URL or a path to a local file.\n"
        "  Note: outdented by mistake.\n"
        "    formats: Comma-separated outputs.\n"
    )
    assert arg_help_in(description, "formats") == "Comma-separated outputs."
    with pytest.raises(AssertionError):
        arg_help_in(description, "Note")


def test_arg_help_finds_the_heading_wherever_it_sits():
    """`Args:` need not own its line, but a description without one is an error."""
    description = "Start here. Args:\n    source: A file.\n    formats: Comma-separated outputs.\n"
    assert arg_help_in(description, "formats") == "Comma-separated outputs."
    with pytest.raises(AssertionError, match="has no Args section"):
        arg_help_in("A description with no argument list at all.", "formats")


def test_every_tool_serves_the_same_args_help_to_a_pre_313_interpreter():
    """The real product text, read the way a 3.10-3.12 harness would receive it.

    3.13 strips the compiled docstring's source indentation; before that the
    literal - which `ast` still holds - was what `fn.__doc__` gave the SDK. So
    the source recovers the older description exactly, with no guessed indent,
    and every tool's `Args:` help must survive the re-indentation unchanged.
    """
    from textflowkit.adapters import mcp_server

    source = Path(mcp_server.__file__).read_text(encoding="utf-8")
    served = {
        node.name: node.body[0].value.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef)
        and node.name in TOOLS
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }

    checked = [name for name in TOOLS if "Args:" in TOOLS[name].description]
    missing = sorted(set(checked) - set(served))
    assert not missing, f"no pre-3.13 docstring literal found for {missing}"
    if sys.version_info >= (3, 13):
        # 3.13 is where the compiler began stripping the source indent, so on
        # this interpreter the two texts really are the two shapes. Older ones
        # keep the literal, and here the check would compare a text with itself.
        assert all(served[name] != TOOLS[name].description for name in checked)
    for name in checked:
        published = TOOLS[name].description
        assert arg_names_in(served[name]) == arg_names_in(published)
        for arg in arg_names_in(published):
            assert arg_help_in(served[name], arg, label=name) == arg_help(name, arg)


@pytest.mark.parametrize("tool", ["transcribe_media", "export_transcript"])
def test_writing_tools_name_every_format_their_validator_accepts(tool):
    """Both validate against SUPPORTED_FORMATS, so both must mention docx and pdf.

    A description that stops at the text formats reads as a restriction the code
    does not impose: `transcribe_media(formats="docx")` is accepted, and the
    tool's own unsupported-format error lists docx and pdf in `available_formats`.
    """
    named = formats_named(arg_help(tool, "formats"))
    missing = sorted(set(SUPPORTED_FORMATS) - named)
    assert not missing, f"{tool} advertises a formats list that omits {missing}"


def test_inline_read_tool_names_only_the_text_formats():
    """`get_transcript` validates TEXT_FORMATS, so docx/pdf belong elsewhere.

    This half of the distinction is already correct; the test keeps a later edit
    from "fixing" it into agreement with the two writing tools.
    """
    named = formats_named(arg_help("get_transcript", "fmt"))
    assert named == set(TEXT_FORMATS)
    assert named.isdisjoint(BINARY_FORMATS)


def test_transcribe_media_accepts_the_binary_formats_its_prose_omits(monkeypatch):
    """Proves the omission is a description defect, not a hidden restriction."""
    from textflowkit.adapters import mcp_server

    captured: dict[str, list[str]] = {}

    def fake_submit(store, request, **kwargs):
        captured["formats"] = list(request.formats)
        return SimpleNamespace(id="job-1", state=JobState.PENDING, source=request.source)

    monkeypatch.setattr(mcp_server, "submit_request", fake_submit)
    out = mcp_server.transcribe_media("local.wav", formats="docx")

    assert "error" not in out
    assert captured["formats"] == ["docx"]


def test_search_description_states_transcript_order_rather_than_newest_first():
    """`search_segments` walks segments in transcript order, oldest end first.

    The description promised "newest-first order preserved from the transcript" -
    a self-contradiction, and not what the core does.
    """
    description = TOOLS["search_transcript"].description
    for claim in ("newest", "most recent first", "reverse order"):
        assert claim not in description.lower(), f"description still claims {claim!r}"
    assert re.search(r"order (they|the segments) appear in", description), (
        "description should say matches follow the order segments appear in the transcript"
    )


def test_core_search_returns_matches_in_transcript_order():
    """The code half of the assertion above, so the prose cannot drift either way."""
    transcript = Transcript(
        source="s",
        language="en",
        segments=[
            Segment(0.0, 1.0, "alpha one"),
            Segment(1.0, 2.0, "beta two"),
            Segment(2.0, 3.0, "alpha three"),
        ],
    )
    matches = search_segments(transcript, "alpha", limit=10, context=0, case_sensitive=False)
    assert [m.index for m in matches] == [0, 2]


def test_list_jobs_description_keeps_its_newest_first_claim():
    """The neighbouring tool's ordering claim is true (`JobStore.recent` sorts by
    insertion sequence), so a sweep that strips "newest" everywhere would be wrong."""
    assert "newest first" in TOOLS["list_jobs"].description.lower()
