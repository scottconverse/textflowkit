"""The bundled Noto fonts carry two attribution strings; the README must show both.

LEG-02: ``OFL-NotoSans.txt`` is the upstream Noto license file exactly as
published, and it opens with the Noto Project Authors notice (2018). The two
binaries that file covers, ``NotoSans.ttf`` and ``NotoSansArabic.ttf``, also
carry an embedded ``name``-table copyright record (nameID 0) naming Google LLC
(2015-2021). The bundled fonts README listed both binaries, their sources and
their hashes, but said nothing about the second string, so a redistributor
reading it would not learn that the two notices differ.

These tests read both strings out of the shipped artifacts - the license text and
the SFNT ``name`` tables - instead of repeating them here, so they fail if either
side moves. The tables are parsed with the standard library alone: ``fontTools``
arrives only through the diarization stack and is not installed by CI's
``.[dev,mcp,http,export]``, so importing it here would pass on a local checkout
and fail in CI.

The disclosure tests are scoped to the README's "Attribution reconciliation"
heading, which is where the reconciliation has to live.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FONTS_DIR = ROOT / "packages/textflowkit-fonts/src/textflowkit_fonts/fonts"
README = FONTS_DIR / "README.md"

# ``OFL-NotoSans.txt`` covers exactly these two binaries.
LICENSED_BINARIES = ("NotoSans.ttf", "NotoSansArabic.ttf")
# The third binary ships under its own license file and its own copyright holder.
SEPARATE_BINARY = "NotoSansSC.ttf"

COPYRIGHT_NAME_ID = 0
WINDOWS_EN_US = (3, 0x0409)
# Legal SFNT "flavours": TrueType outlines, the ``true`` tag, and CFF outlines.
SFNT_VERSIONS = (b"\x00\x01\x00\x00", b"true", b"OTTO")

# The README has to say both notices are kept, not that one wins.
RETAINED_RE = re.compile(r"\bboth\b[^.]{0,160}\b(?:retain|preserv|kept|keep)\w*", re.IGNORECASE)
# ...and that reconciling two published strings is not itself a legal ruling.
NOT_A_CONCLUSION_RE = re.compile(
    r"\bnot a\b[^.\n]{0,60}\blegal\b"
    r"|\bnot\b[^.\n]{0,40}\bfinding of\b"
    r"|\bdoes not\b[^.\n]{0,40}\bconclude\b",
    re.IGNORECASE,
)

# ATX (``## Title``) and setext (``Title`` over ``====`` / ``----``) headings.
HEADING_RE = re.compile(
    r"^(?:(?P<hashes>#{1,6})[ \t]+(?P<atx>.+?)[ \t]*"
    r"|(?P<setext>.+?)[ \t]*\n(?P<underline>=+|-+)[ \t]*)$",
    re.MULTILINE,
)
COPYRIGHT_LINE_RE = re.compile(r"^[ \t]*Copyright\b.*$", re.MULTILINE)
ATTRIBUTION_HEADING = "Attribution reconciliation"


# --- SFNT parsing (standard library only, bounded by explicit length checks) ---


def _sfnt_tables(data: bytes) -> dict[str, tuple[int, int]]:
    """Offset and length of every table in a single-font SFNT file."""
    if len(data) < 12:
        raise AssertionError("font file is shorter than an SFNT offset table")
    if data[:4] not in SFNT_VERSIONS:
        raise AssertionError(f"not a single-font SFNT file: version tag {data[:4]!r}")
    (num_tables,) = struct.unpack_from(">H", data, 4)
    if not 0 < num_tables < 512:
        raise AssertionError(f"implausible SFNT table count {num_tables}")
    if 12 + 16 * num_tables > len(data):
        raise AssertionError("SFNT table directory runs past the end of the file")
    tables: dict[str, tuple[int, int]] = {}
    for index in range(num_tables):
        tag, _checksum, offset, length = struct.unpack_from(">4sIII", data, 12 + 16 * index)
        if offset + length > len(data):
            raise AssertionError(f"table {tag!r} runs past the end of the file")
        tables[tag.decode("latin-1")] = (offset, length)
    return tables


def _decode_name(platform: int, raw: bytes) -> str:
    if platform in (0, 3):
        return raw.decode("utf-16-be", "replace")
    if platform == 1:
        return raw.decode("mac-roman", "replace")
    return raw.decode("latin-1", "replace")


def _name_records(data: bytes) -> list[tuple[int, int, int, int, str]]:
    """``(platform, encoding, language, name_id, text)`` for every name record."""
    try:
        offset, length = _sfnt_tables(data)["name"]
    except KeyError as exc:
        raise AssertionError("font has no name table") from exc
    if length < 6:
        raise AssertionError("name table is too short to hold its header")
    fmt, count, strings_offset = struct.unpack_from(">HHH", data, offset)
    if fmt not in (0, 1):
        raise AssertionError(f"unexpected name table format {fmt}")
    if 6 + 12 * count > length:
        raise AssertionError("name records run past the end of the name table")
    if strings_offset > length:
        raise AssertionError("name string storage starts past the end of the name table")
    records = []
    for index in range(count):
        platform, encoding, language, name_id, size, string_offset = struct.unpack_from(
            ">HHHHHH", data, offset + 6 + 12 * index
        )
        start = offset + strings_offset + string_offset
        end = start + size
        if end > offset + length:
            raise AssertionError(
                f"name record {index} (nameID {name_id}) runs past the end of the name table"
            )
        records.append(
            (platform, encoding, language, name_id, _decode_name(platform, data[start:end]))
        )
    return records


def _embedded_copyright(font: Path) -> str:
    """The nameID 0 (copyright) string a font binary actually carries."""
    candidates = [
        (platform, language, text)
        for platform, _encoding, language, name_id, text in _name_records(font.read_bytes())
        if name_id == COPYRIGHT_NAME_ID
    ]
    assert candidates, f"{font.name} carries no nameID 0 (copyright) record"
    for platform, language, text in candidates:
        if (platform, language) == WINDOWS_EN_US:
            return text
    return candidates[0][2]


def _license_copyright(path: Path) -> str:
    """The first copyright line of a shipped license file."""
    match = COPYRIGHT_LINE_RE.search(path.read_text(encoding="utf-8"))
    assert match, f"{path.name} states no copyright line"
    return match.group(0).strip()


def _headings(text: str) -> list[tuple[int, str, int, int]]:
    """``(level, title, start, end)`` for every ATX or setext heading, in order."""
    headings = []
    for match in HEADING_RE.finditer(text):
        if match.group("hashes"):
            level, title = len(match.group("hashes")), match.group("atx")
        else:
            level = 1 if match.group("underline").startswith("=") else 2
            title = match.group("setext")
        headings.append((level, title.strip(), match.start(), match.end()))
    return headings


def _section(text: str, title: str) -> str:
    """Body of the heading ``title``, up to the next heading of the same or higher level."""
    headings = _headings(text)
    for index, (level, name, _start, end) in enumerate(headings):
        if name != title:
            continue
        for later_level, _later_name, later_start, _later_end in headings[index + 1 :]:
            if later_level <= level:
                return text[end:later_start]
        return text[end:]
    raise AssertionError(
        f"heading {title!r} not found; headings present: {[heading[1] for heading in headings]}"
    )


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(scope="module")
def license_notice() -> str:
    return _license_copyright(FONTS_DIR / "OFL-NotoSans.txt")


@pytest.fixture(scope="module")
def embedded_notices() -> dict[str, str]:
    return {name: _embedded_copyright(FONTS_DIR / name) for name in LICENSED_BINARIES}


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def attribution_note(readme: str) -> str:
    return _section(readme, ATTRIBUTION_HEADING)


# --- anchors: the two strings really do differ, and SC really is separate ------


def test_the_two_licensed_binaries_embed_a_holder_the_license_header_does_not_state(
    license_notice: str, embedded_notices: dict[str, str]
) -> None:
    """Anchor: the ambiguity LEG-02 names is present in the shipped bytes."""
    assert len(set(embedded_notices.values())) == 1, (
        "the two binaries covered by OFL-NotoSans.txt no longer share one embedded copyright "
        f"record; found {embedded_notices}"
    )
    embedded = next(iter(embedded_notices.values()))
    assert embedded != license_notice, (
        "the embedded copyright record now matches the upstream license header, so the "
        "disclosure rule in this file is no longer anchored on two differing notices"
    )
    assert "Noto Project Authors" in license_notice, (
        f"OFL-NotoSans.txt no longer opens with the upstream Noto Project Authors notice: "
        f"{license_notice!r}"
    )
    assert "Google" in embedded, (
        f"the embedded copyright record no longer names Google: {embedded!r}"
    )


def test_noto_sans_sc_ships_a_notice_of_its_own(embedded_notices: dict[str, str]) -> None:
    """Anchor: the third binary's notice is a different holder, and must not be folded in."""
    sc_notice = _embedded_copyright(FONTS_DIR / SEPARATE_BINARY)
    sc_license_notice = _license_copyright(FONTS_DIR / "OFL-NotoSansSC.txt")
    assert sc_notice not in embedded_notices.values(), (
        f"{SEPARATE_BINARY}'s embedded copyright record now matches the first two binaries; "
        "the separate-notice rule in this file needs revisiting"
    )
    assert "Adobe" in sc_notice and "Adobe" in sc_license_notice, (
        f"{SEPARATE_BINARY} and OFL-NotoSansSC.txt no longer both name Adobe: "
        f"{sc_notice!r} / {sc_license_notice!r}"
    )


# --- the disclosure the README was missing ------------------------------------


def test_readme_discloses_both_notices_for_the_two_licensed_binaries(
    attribution_note: str, license_notice: str, embedded_notices: dict[str, str]
) -> None:
    assert license_notice in attribution_note, (
        f"the {ATTRIBUTION_HEADING!r} note does not quote the upstream license copyright "
        f"line it is reconciling: {license_notice!r}"
    )
    for name, notice in embedded_notices.items():
        assert notice in attribution_note, (
            f"the {ATTRIBUTION_HEADING!r} note does not quote {name}'s embedded copyright "
            f"record: {notice!r}"
        )
        assert name in attribution_note, (
            f"the {ATTRIBUTION_HEADING!r} note does not name {name} as carrying that record"
        )


def test_readme_keeps_both_notices_and_stays_attribution_not_a_legal_conclusion(
    attribution_note: str,
) -> None:
    assert RETAINED_RE.search(attribution_note), (
        f"the {ATTRIBUTION_HEADING!r} note does not say both notices are retained as published"
    )
    assert NOT_A_CONCLUSION_RE.search(attribution_note), (
        f"the {ATTRIBUTION_HEADING!r} note does not say that reconciling two published strings "
        "is not itself a legal conclusion"
    )


def test_readme_does_not_attribute_the_noto_sans_notice_to_noto_sans_sc(
    attribution_note: str, embedded_notices: dict[str, str]
) -> None:
    """The third binary must not be swept into the first two binaries' notice."""
    for notice in embedded_notices.values():
        for line in attribution_note.splitlines():
            if notice in line:
                assert SEPARATE_BINARY not in line, (
                    f"the {ATTRIBUTION_HEADING!r} note attaches {SEPARATE_BINARY} to the "
                    f"first two binaries' notice: {line.strip()!r}"
                )
