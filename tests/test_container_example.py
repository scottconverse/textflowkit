"""The container example must describe the server it claims to run.

Outside-review C3 asks for a Dockerfile and a Compose example for the JSON HTTP
adapter: ffmpeg and a JavaScript runtime in the image, production settings and an
authenticated healthcheck in Compose, and no credentials in the repository.

There is no container engine on the machine these were written on, so nothing
here starts a container. These are static contract checks over the shipped
files, strong enough to catch a drifted or half-written example and honest about
what they do not prove: whether the image builds, starts, and passes its
healthcheck in a real engine is **unverified** here and belongs to a hosted CI
gate (see `reports/U44-c3-container-example.md`).

`compose.yaml` is read with the strict subset parser below rather than PyYAML,
for the same reason `tests/test_publish_workflow_gate.py` scans workflow text by
hand: PyYAML is not a dependency of the package or of the `dev` extra, so a
parser import could fail on a CI matrix leg that has no diarization stack. The
parser refuses every construct it cannot verify - flow collections, block
scalars, anchors, tags, tabs, inline mapping sequence items - so a file it
accepts is known to be inside the subset, and a file that leaves the subset
fails loudly instead of being silently misread.

Two of the checks are tied to the environment rather than to a remembered
number: the JavaScript-runtime floor in the Dockerfile is compared against
yt-dlp's own `MIN_SUPPORTED_VERSION`, and the volume targets are compared
against the root paths the service sets.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

DOCKERFILE = "Dockerfile"
COMPOSE = "compose.yaml"
DOCKERIGNORE = ".dockerignore"
ENV_EXAMPLE = ".env.example"
DOCS = "docs/adapters.md"

# The one service this example ships. SQLite has a single owning process, so a
# second service would reap the first one's live jobs.
SERVICE = "textflowkit-http"

# Paths inside the container. They are also what the Dockerfile's ENV defaults
# set, which is why the Compose file repeats them: the volumes have to land on
# the same directories the service is configured to use.
CONTAINER_INPUT = "/srv/textflowkit/input"
CONTAINER_OUTPUT = "/srv/textflowkit/output"
CONTAINER_WORK = "/srv/textflowkit/work"
CONTAINER_STATE = "/srv/textflowkit/state"

# Required by `validate_production_config()` (core/service.py). Named here
# because they are the operator-facing contract, not an implementation detail.
REQUIRED_PRODUCTION_SETTINGS = (
    "TEXTFLOWKIT_PROFILE",
    "TEXTFLOWKIT_API_TOKEN",
    "TEXTFLOWKIT_INPUT_ROOT",
    "TEXTFLOWKIT_OUTPUT_ROOT",
    "TEXTFLOWKIT_WORK_ROOT",
    "TEXTFLOWKIT_DB",
)

# Every construct this subset parser refuses, so a file it accepts cannot be
# relying on one. The leading characters are YAML indicators; `>` and `|` open
# block scalars, `&`/`*` anchors and aliases, `!` and `%` tags and directives.
UNSUPPORTED_LEADING = "'[{|>&*!%@`"


class YamlSubsetError(AssertionError):
    """`compose.yaml` left the subset this parser is able to verify."""


# --- reading the shipped files -------------------------------------------


def _read(name: str) -> str:
    path = ROOT / name
    assert path.is_file(), f"{name} is missing from the repository root ({path})"
    return path.read_text(encoding="utf-8")


def _instructions(name: str) -> list[tuple[str, str]]:
    """(COMMAND, argument) per Dockerfile instruction, continuations joined.

    A comment or a blank line inside a continuation is refused rather than
    silently dropped: Docker forbids it, and accepting it would let this test
    read a file Docker would reject.
    """
    out: list[tuple[str, str]] = []
    buffer = ""
    continued = False
    for number, raw in enumerate(_read(name).splitlines(), 1):
        line = raw.strip()
        if continued:
            assert line and not line.startswith("#"), (
                f"{name}:{number}: a blank line or comment inside a line continuation"
            )
        else:
            if not line or line.startswith("#"):
                continue
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continued = True
            continue
        buffer += line
        command, _, argument = buffer.partition(" ")
        out.append((command.upper(), argument.strip()))
        buffer = ""
        continued = False
    assert not buffer, f"{name}: a line continuation runs past the end of the file"
    return out


def _instruction(instructions: list[tuple[str, str]], command: str) -> str:
    found = [argument for name, argument in instructions if name == command]
    assert len(found) == 1, f"expected exactly one {command} instruction, found {len(found)}"
    return found[0]


def _runs(instructions: list[tuple[str, str]], marker: str) -> str:
    """The single RUN whose text contains `marker`."""
    found = [argument for name, argument in instructions if name == "RUN" and marker in argument]
    assert len(found) == 1, f"expected one RUN containing {marker!r}, found {len(found)}"
    return found[0]


def _arg(instructions: list[tuple[str, str]], name: str) -> str | None:
    pattern = re.compile(rf"^{re.escape(name)}=(?P<value>\S+)$")
    for command, argument in instructions:
        if command != "ARG":
            continue
        match = pattern.match(argument)
        if match:
            return match.group("value")
    return None


def _exec_form(argument: str, marker: str | None = None) -> list[str]:
    """The JSON argv of an exec-form instruction, after `marker` when given.

    `CMD ["a", "b"]` carries the array as its whole argument; a `HEALTHCHECK`
    carries options and then `CMD [...]`, so the marker says where the array
    starts.
    """
    payload = argument.strip()
    if marker is not None:
        index = payload.find(marker)
        assert index != -1, f"no {marker!r} in {argument!r}"
        payload = payload[index + len(marker):].strip()
    assert payload.startswith("["), f"{marker or 'the instruction'} must use the exec form"
    argv = json.loads(payload)
    assert isinstance(argv, list) and all(isinstance(item, str) for item in argv), argv
    return argv


# --- the strict subset parser for compose.yaml ----------------------------


def _parse_scalar(raw: str, number: int) -> str:
    """A plain scalar or a double-quoted string; nothing else."""
    text = raw.strip()
    assert text, f"compose.yaml:{number}: a key with an empty value"
    if text.startswith('"'):
        assert text.endswith('"') and len(text) > 1, (
            f"compose.yaml:{number}: unterminated double-quoted scalar"
        )
        return text[1:-1]
    if text[0] in UNSUPPORTED_LEADING:
        raise YamlSubsetError(
            f"compose.yaml:{number}: {text[0]!r} opens a construct this subset parser "
            "does not verify; rewrite the value as a plain or double-quoted scalar"
        )
    return text


def _tokens(text: str) -> list[tuple[int, str, int]]:
    """(indent, content, line number) for every line that is not blank or a comment."""
    out: list[tuple[int, str, int]] = []
    for number, raw in enumerate(text.splitlines(), 1):
        body = raw.rstrip()
        if not body.strip() or body.lstrip().startswith("#"):
            continue
        indent = len(body) - len(body.lstrip(" "))
        assert "\t" not in body[:indent], f"compose.yaml:{number}: tabs cannot indent YAML"
        out.append((indent, body.strip(), number))
    assert out, "compose.yaml carries no content"
    return out


def _parse_sequence(
    tokens: list[tuple[int, str, int]], index: int, indent: int
) -> tuple[list[str], int]:
    items: list[str] = []
    while index < len(tokens):
        level, content, number = tokens[index]
        if level != indent or not content.startswith("- "):
            break
        body = content[2:].strip()
        assert body, (
            f"compose.yaml:{number}: a sequence item must carry its scalar on the same line"
        )
        if ":" in body and not body.startswith('"'):
            raise YamlSubsetError(
                f"compose.yaml:{number}: inline mapping sequence items are not supported; "
                "put the mapping on its own indented lines"
            )
        items.append(_parse_scalar(body, number))
        index += 1
    return items, index


def _parse_mapping(
    tokens: list[tuple[int, str, int]], index: int, indent: int
) -> tuple[dict[str, object], int]:
    mapping: dict[str, object] = {}
    while index < len(tokens):
        level, content, number = tokens[index]
        if level < indent:
            break
        assert level == indent, f"compose.yaml:{number}: unexpected indentation"
        if content.startswith("- "):
            break
        key, separator, rest = content.partition(":")
        assert separator, f"compose.yaml:{number}: expected 'key: value'"
        name = _parse_scalar(key, number)
        index += 1
        rest = rest.strip()
        if rest:
            mapping[name] = _parse_scalar(rest, number)
            continue
        if index < len(tokens) and tokens[index][0] > indent:
            mapping[name], index = _parse_node(tokens, index, tokens[index][0])
            continue
        if index < len(tokens) and tokens[index][1].startswith("- "):
            raise YamlSubsetError(
                f"compose.yaml:{number}: a sequence at the key's own indentation is not "
                "supported; indent it under the key"
            )
        mapping[name] = None
    return mapping, index


def _parse_node(
    tokens: list[tuple[int, str, int]], index: int, indent: int
) -> tuple[object, int]:
    if tokens[index][1].startswith("- "):
        return _parse_sequence(tokens, index, indent)
    return _parse_mapping(tokens, index, indent)


def _compose() -> dict[str, object]:
    tokens = _tokens(_read(COMPOSE))
    assert tokens[0][0] == 0, "compose.yaml: the first key must be at the start of the line"
    document, index = _parse_mapping(tokens, 0, 0)
    assert index == len(tokens), (
        f"compose.yaml: content this parser did not consume at line {tokens[index][2]}"
    )
    return document


def _service() -> dict[str, object]:
    services = _compose().get("services")
    assert isinstance(services, dict), "compose.yaml must declare a `services:` mapping"
    assert list(services) == [SERVICE], (
        f"expected exactly one service ({SERVICE!r}), found {sorted(services)}"
    )
    service = services[SERVICE]
    assert isinstance(service, dict), f"service {SERVICE!r} must be a mapping"
    return service


def _mapping(node: object, label: str) -> dict[str, object]:
    assert isinstance(node, dict), f"{label} must be a mapping"
    return node


def _sequence(node: object, label: str) -> list[str]:
    assert isinstance(node, list), f"{label} must be a sequence"
    return node


def _mounts(service: dict[str, object]) -> list[tuple[str, str, str]]:
    """(source, target, mode) per volume entry; `mode` is `ro` or `rw`."""
    entries = _sequence(service.get("volumes"), "service volumes")
    mounts: list[tuple[str, str, str]] = []
    for entry in entries:
        parts = entry.split(":")
        assert len(parts) in (2, 3), f"unexpected volume entry {entry!r}"
        source, target = parts[0], parts[1]
        mode = parts[2] if len(parts) == 3 else "rw"
        mounts.append((source, target, mode))
    return mounts


# --- the Dockerfile -------------------------------------------------------


def test_the_image_is_a_single_pinned_stage() -> None:
    instructions = _instructions(DOCKERFILE)
    bases = [argument for command, argument in instructions if command == "FROM"]
    assert len(bases) == 1, f"expected one build stage, found {bases}"
    # `COPY --from=<image>` may name an external image; that is not a second stage.
    base = bases[0].split()[0]
    assert ":" in base, f"the base image {base!r} is not pinned to a tag"
    assert not base.endswith(":latest"), f"the base image {base!r} floats on :latest"


def test_the_image_installs_ffmpeg_and_a_gated_javascript_runtime() -> None:
    instructions = _instructions(DOCKERFILE)
    packages = _runs(instructions, "apt-get install")
    assert "ffmpeg" in packages, "the image must install ffmpeg: decode needs it in every profile"
    # ffprobe ships in the same package, but the production profile also refuses a
    # job it cannot probe, so the pair is worth naming.
    assert "ca-certificates" in packages, "URL acquisition needs a trust store"
    node_copies = [
        argument for command, argument in instructions
        if command == "COPY" and "node" in argument and "/usr/local/bin/node" in argument
    ]
    assert len(node_copies) == 1, (
        "the image must take a JavaScript runtime from a pinned external image; "
        f"found {node_copies}"
    )
    # The floor is enforced at build time, so an image whose runtime is too old
    # fails the build instead of shipping a runtime yt-dlp will refuse.
    gate = _runs(instructions, "process.versions.node")
    assert "${NODE_MIN_MAJOR}" in gate, "the gate must read the declared floor, not a literal"
    assert "node --version" in gate, "the build log must record the runtime it accepted"


def test_the_javascript_floor_matches_the_installed_yt_dlp() -> None:
    """yt-dlp's Node provider refuses a runtime below its own floor.

    The number is read from the installed yt-dlp rather than remembered, so a
    yt-dlp upgrade that raises the floor fails here instead of leaving the
    Dockerfile pinned to a version yt-dlp no longer accepts. A yt-dlp that moved
    this module is reported as a failure, not skipped.
    """
    try:
        from yt_dlp.utils import _jsruntime
    except ImportError as exc:  # pragma: no cover - depends on the installed yt-dlp
        pytest.fail(f"yt-dlp no longer exposes utils._jsruntime: {exc}")
    runtime = getattr(_jsruntime, "NodeJsRuntime", None)
    assert runtime is not None, "yt-dlp's jsruntime module no longer defines NodeJsRuntime"
    floor_major = runtime.MIN_SUPPORTED_VERSION[0]

    declared = _arg(_instructions(DOCKERFILE), "NODE_MIN_MAJOR")
    assert declared is not None, "the Dockerfile must declare NODE_MIN_MAJOR"
    assert int(declared) == floor_major, (
        f"the Dockerfile gates Node at {declared}, but the installed yt-dlp requires "
        f"{floor_major}.{'.'.join(str(part) for part in runtime.MIN_SUPPORTED_VERSION[1:])}"
    )


def test_the_image_installs_the_package_with_the_http_extra() -> None:
    install = _runs(_instructions(DOCKERFILE), "pip install")
    assert "http" in install, "the HTTP adapter needs the `http` extra"
    assert "--no-cache-dir" in install, "the image must not carry a pip cache"
    for copied in ("pyproject.toml", "README.md", "LICENSE"):
        assert copied in _read(DOCKERFILE), (
            f"the wheel build reads {copied} from the build context"
        )


def test_the_container_runs_as_a_nonroot_user_that_owns_its_roots() -> None:
    instructions = _instructions(DOCKERFILE)
    user = _instruction(instructions, "USER").strip()
    assert user not in {"root", "0", "0:0"}, f"the runtime user must not be root: {user!r}"
    uid = user.split(":")[0]
    assert uid.isdecimal() and int(uid) > 0, f"expected a numeric non-root uid, got {user!r}"

    created = _runs(instructions, "useradd")
    assert f"--uid {uid}" in created, f"the {uid} the container runs as must be created: {created}"
    chowned = [c for c in (CONTAINER_INPUT, CONTAINER_OUTPUT, CONTAINER_WORK, CONTAINER_STATE)
               if c in created]
    assert sorted(chowned) == sorted(
        (CONTAINER_INPUT, CONTAINER_OUTPUT, CONTAINER_WORK, CONTAINER_STATE)
    ), f"every read/write root must be created owned by that user, found {chowned}"


def test_the_server_is_started_on_the_container_interface_with_the_remote_opt_in() -> None:
    instructions = _instructions(DOCKERFILE)
    argv = _exec_form(_instruction(instructions, "CMD"))
    assert argv[0] == "textflowkit-http", f"expected the JSON HTTP console script, got {argv}"
    assert "--host" in argv and argv[argv.index("--host") + 1] == "0.0.0.0", (
        "the server must listen on the container interface so the published port reaches it"
    )
    # 0.0.0.0 inside the container is a non-loopback bind, which the adapter
    # refuses unless the operator opts in. The host port stays loopback-only.
    assert "--allow-remote" in argv, "binding beyond loopback is refused without the opt-in"
    exposed = _instruction(instructions, "EXPOSE")
    assert exposed == str(argv[argv.index("--port") + 1]), (
        f"EXPOSE {exposed} must name the port the server is started on"
    )


def test_the_image_healthcheck_authenticates_without_a_token_in_argv() -> None:
    instructions = _instructions(DOCKERFILE)
    argument = _instruction(instructions, "HEALTHCHECK")
    assert "--start-period" in argument, "the probe needs a start period before it can fail the image"
    argv = _exec_form(argument, "CMD")
    script = " ".join(argv)
    assert "TEXTFLOWKIT_API_TOKEN" in script, "the production profile answers 401 without a token"
    assert "os.environ" in script, (
        "the token must come from the process environment; an argument is visible to `ps`"
    )
    assert "Authorization" in script and "Bearer" in script, "the probe must send the Bearer header"
    assert "/health" in script, "the probe must hit the health endpoint"


# `TEXTFLOWKIT_API_TOKEN=...` (a Dockerfile ENV) or `TEXTFLOWKIT_API_TOKEN: ...`
# (Compose). A *reference* to the variable - the healthcheck reads it from the
# environment - is not an assignment and must not be read as one.
TOKEN_ASSIGNMENT = re.compile(r"TEXTFLOWKIT_API_TOKEN\s*(?::=|:|=)\s*(?P<value>.*)$")


def test_neither_file_carries_a_production_token() -> None:
    for name in (DOCKERFILE, COMPOSE, ENV_EXAMPLE):
        for line in _read(name).splitlines():
            if line.strip().startswith("#"):
                continue
            match = TOKEN_ASSIGNMENT.search(line)
            if match is None:
                continue
            value = match.group("value").strip().strip('"')
            # The value may be an interpolation the operator supplies, or an
            # empty placeholder. It may never be a literal secret.
            assert value == "" or value.startswith("${"), (
                f"{name} appears to embed a token value: {line.strip()!r}"
            )
    assert "${TEXTFLOWKIT_API_TOKEN:?" in _read(COMPOSE), (
        "Compose must refuse to start when the operator has not supplied a token"
    )


def test_the_build_context_keeps_the_license_source_and_docs() -> None:
    patterns = [
        line.strip()
        for line in _read(DOCKERIGNORE).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not [pattern for pattern in patterns if pattern.startswith("!") and "LICENSE" in pattern]
    for required in ("LICENSE", "README.md", "pyproject.toml", "src", "docs"):
        assert required not in patterns, f"{DOCKERIGNORE} must not exclude {required}"
    for local in (".git", ".venv", ".env"):
        assert local in patterns, f"{DOCKERIGNORE} must keep {local} out of the build context"
    # The wheel build packs this asset (`artifacts` in pyproject.toml); a media
    # filter that drops it would ship an image with a silently empty asset set.
    assert "!src/textflowkit/assets/selftest-speech.wav" in patterns, (
        "the self-test asset must survive the media filter"
    )


# --- compose.yaml ---------------------------------------------------------


def test_the_example_runs_the_production_profile_with_every_mandatory_setting() -> None:
    service = _service()
    environment = _mapping(service.get("environment"), "service environment")
    missing = [name for name in REQUIRED_PRODUCTION_SETTINGS if name not in environment]
    assert not missing, f"the production profile refuses to start without {missing}"
    assert environment["TEXTFLOWKIT_PROFILE"] == "production"
    store = environment["TEXTFLOWKIT_DB"]
    assert store != ":memory:", "production requires an on-disk job store"
    assert store.startswith("/"), f"the job store must be an absolute container path, got {store}"


def test_the_host_port_is_published_to_loopback_only() -> None:
    ports = _sequence(_service().get("ports"), "service ports")
    assert len(ports) == 1, f"expected one published port, found {ports}"
    host, container = ports[0].rsplit(":", 2)[0], ports[0].rsplit(":", 2)[2]
    assert host == "127.0.0.1", (
        f"the host side must be loopback-only; a bare port publishes on 0.0.0.0 ({ports[0]!r})"
    )
    assert container == "8767", f"expected the adapter's default port, got {container!r}"


def test_the_durable_store_and_its_roots_are_persisted_across_restarts() -> None:
    service = _service()
    environment = _mapping(service.get("environment"), "service environment")
    mounts = {target: (source, mode) for source, target, mode in _mounts(service)}
    store = environment["TEXTFLOWKIT_DB"]
    state = store.rsplit("/", 1)[0]
    for root in (environment["TEXTFLOWKIT_OUTPUT_ROOT"], environment["TEXTFLOWKIT_WORK_ROOT"], state):
        assert root in mounts, f"nothing persists {root}: a restart would lose it"
        source, _ = mounts[root]
        assert not source.startswith("/tmp") and not source.startswith("tmpfs"), (
            f"{root} must persist on a volume, not in a scratch path ({source!r})"
        )
    source, mode = mounts[environment["TEXTFLOWKIT_INPUT_ROOT"]]
    assert mode == "ro", f"the media input is read-only ({source!r} would be writable)"
    assert not source.startswith("/tmp"), "the input must be a mount the operator can fill"


def test_the_compose_healthcheck_authenticates_like_the_image_one() -> None:
    healthcheck = _mapping(_service().get("healthcheck"), "service healthcheck")
    argv = _sequence(healthcheck.get("test"), "healthcheck test")
    assert argv[0] == "CMD", f"the probe must run the argv form, not a shell string: {argv}"
    script = " ".join(argv)
    assert "TEXTFLOWKIT_API_TOKEN" in script and "os.environ" in script, (
        "the probe must read the token from the environment"
    )
    assert "Bearer" in script and "/health" in script, "the probe must send the Bearer header"
    assert healthcheck.get("start_period"), "the probe needs a start period"
    assert healthcheck.get("retries"), "the probe needs a bounded retry count"

    image_argv = _exec_form(_instruction(_instructions(DOCKERFILE), "HEALTHCHECK"), "CMD")
    assert argv[1:] == image_argv, (
        "the image and the Compose file must run the same probe, or one of them is untested"
    )


def test_the_example_bounds_resources_and_keeps_one_owning_process() -> None:
    service = _service()
    deploy = _mapping(service.get("deploy"), "service deploy")
    limits = _mapping(_mapping(deploy.get("resources"), "deploy resources").get("limits"),
                      "deploy resource limits")
    assert limits.get("memory"), "a transcription job can exhaust the host without a memory bound"
    assert limits.get("cpus"), "an unbounded CPU share starves the host it shares"
    assert str(deploy.get("replicas", 1)) == "1", (
        "SQLite has one owning process: two replicas would reap each other's live jobs"
    )
    assert service.get("restart"), "a crashed server should come back"
    # The two settings that keep a runaway decode from taking the container with it.
    assert service.get("pids_limit") or service.get("ulimits"), "bound the process count"


def test_the_example_does_not_imply_egress_it_does_not_ship() -> None:
    environment = _mapping(_service().get("environment"), "service environment")
    assert "TEXTFLOWKIT_EGRESS_PROXY" not in environment, (
        "no SSRF-filtering proxy is bundled; setting one here would be a lie. "
        "Unset, production URL jobs fail closed and local files still work."
    )
    command = " ".join(
        str(part) for part in _sequence(_service().get("command") or [], "service command")
    )
    assert "TEXTFLOWKIT_API_TOKEN" not in command, (
        "the token must not reach a command line, where `ps` and `docker inspect` read it"
    )


def test_the_env_example_documents_the_requirement_without_supplying_one() -> None:
    text = _read(ENV_EXAMPLE)
    assert "TEXTFLOWKIT_API_TOKEN=" in text, "the example must name the variable it needs"
    assert "openssl rand" in text, "it must say how to generate a value"


# --- what the token's exposure claims are allowed to say ------------------
#
# An environment variable keeps the token out of process arguments. It does not
# make the value secret: Docker records it in the container's configuration,
# where `docker inspect` and `docker compose config` render it, and on Linux
# `/proc/<pid>/environ` exposes it to anything running as the same user.
# Documentation that stops at "not an argument" reads as though the token were
# protected, which is how an operator ends up treating one shared value as a
# secret store. The `ps` benefit and the inspection caveat travel together.
#
# (Documented Docker and Linux behaviour; not measured here, because no
# container engine is installed on the machine these files were written on.)

# Phrases asserting more privacy than an environment variable provides. The
# first is the one this unit shipped before the audit return; the rest are the
# obvious ways to write it again.
TOKEN_PRIVACY_CLAIMS = (
    "out of the container's inspect data",
    "stays out of docker inspect",
    "not visible to docker inspect",
    "invisible to docker inspect",
)


def _normalised_text(value: str) -> str:
    """Text lowercased, backticks dropped, whitespace collapsed, for claim checks."""
    return " ".join(value.lower().replace("`", "").split())


def _normalised(name: str) -> str:
    """`_normalised_text` over a shipped file."""
    return _normalised_text(_read(name))


def test_no_shipped_file_claims_an_environment_variable_hides_the_token() -> None:
    for name in (DOCKERFILE, COMPOSE, ENV_EXAMPLE, DOCS):
        text = _normalised(name)
        for claim in TOKEN_PRIVACY_CLAIMS:
            assert claim not in text, (
                f"{name} claims more privacy than an environment variable gives "
                f"({claim!r}): the value sits in the container's configuration, where "
                "`docker inspect` renders it"
            )


def test_every_ps_claim_carries_the_inspection_caveat() -> None:
    for name in (DOCKERFILE, COMPOSE, ENV_EXAMPLE, DOCS):
        raw = _read(name)
        if "`ps`" not in raw:
            continue
        assert "inspect" in _normalised(name), (
            f"{name} says the token stays out of `ps` and stops there. Docker records an "
            "environment variable in the container's configuration, so say that too"
        )


def test_the_env_example_names_who_can_read_the_token() -> None:
    text = _normalised(ENV_EXAMPLE)
    for required, why in (
        ("docker inspect", "the container's configuration carries the value"),
        ("compose config", "the resolved Compose file carries the value"),
        ("daemon", "access to the daemon is the exposure"),
        ("proc", "`/proc/<pid>/environ` exposes it inside the container"),
        ("git", "`.env` is ignored by git, which is not the same as secret"),
        ("secret", "one shared token is not a secret store, and must not be read as one"),
    ):
        assert required in text, f".env.example must say it: {why} ({required!r} is missing)"


# --- how the example tells an operator to check it ------------------------
#
# `docker compose` reads `.env` to interpolate the Compose file; it does not
# export those values into the caller's shell. A documented
# `curl -H "Authorization: Bearer $TEXTFLOWKIT_API_TOKEN"` therefore sends an
# empty bearer, and the obvious way to make it work - exporting the variable -
# puts the value into curl's argv, which is the exposure the token note promises
# to keep out of a command line. Every documented check has to run inside
# Compose, where the token is already an environment variable of the process
# that owns it.

CONTAINER_SECTION = "### Container example (Dockerfile and Compose)"

SHELL_TOKEN_BEARER = re.compile(r"Bearer\s+\$\{?TEXTFLOWKIT_API_TOKEN")

EXEC_PROBE = re.compile(r'docker compose exec textflowkit-http python -c "(?P<script>[^"]+)"')

# A host HTTP client *invoked* in the example's own instructions - a command line
# that starts with the client, not a mention of one in prose. `curl` from the host
# is what cannot work here: the token is in `.env`, not in the caller's shell.
HOST_HTTP_CALL = re.compile(
    r"(?im)^[\s#$>]*(?:curl|wget|invoke-webrequest|invoke-restmethod)\b"
)

# What the operator-visible check actually reports. `docker compose ps` shows the
# container's own health, which is not a call from the host; the example has to
# say so, in one of these forms.
PROBE_SCOPES = (
    "container's health status",
    "container health status",
    "container's own healthcheck",
    "container's own probe",
    "probe the container runs",
    "runs in the container",
    "not a call from the host",
    "not a host http call",
)


def _container_docs() -> str:
    """The `### Container example` section of docs/adapters.md, up to the next heading."""
    text = _read(DOCS)
    start = text.index(CONTAINER_SECTION)
    rest = text[start + len(CONTAINER_SECTION):]
    end = rest.find("\n### ")
    return rest if end == -1 else rest[:end]


def test_no_documented_command_expands_the_token_from_the_callers_shell() -> None:
    for name, text in ((COMPOSE, _read(COMPOSE)), (DOCS, _container_docs())):
        match = SHELL_TOKEN_BEARER.search(text)
        assert match is None, (
            f"{name} documents a bearer token expanded by the caller's shell "
            f"({match.group(0)!r}): Compose does not export `.env` into that shell, so "
            "the token would be empty - and exporting it puts the value in the client's "
            "argv. Run the probe inside the container instead"
        )


def test_the_documented_check_runs_through_compose() -> None:
    for name, text in ((COMPOSE, _read(COMPOSE)), (DOCS, _container_docs())):
        assert "docker compose ps" in text or "docker compose exec" in text, (
            f"{name} must show a check that works with only `.env` filled: the "
            "container's health status, or a probe executed inside the container"
        )


def test_the_documented_probe_is_the_one_the_container_runs() -> None:
    documented = EXEC_PROBE.search(_container_docs())
    assert documented is not None, (
        "docs/adapters.md must show the on-demand probe verbatim, not a paraphrase"
    )
    shipped = _sequence(_mapping(_service().get("healthcheck"), "service healthcheck").get("test"),
                        "healthcheck test")[-1]
    assert documented.group("script") == shipped, (
        "the probe the operator is told to run must be the probe the container runs; "
        "a copy that drifts is a command nobody has exercised"
    )


# What the prose promises the documented probe shows. The probe is the
# container's own `healthcheck.test` script, which asserts on the response; it has
# to keep asserting, because byte-equality with the shipped probe is what makes
# the documented command a command the container actually runs. So the text
# around it describes that, rather than promising a body nobody printed.
BODY_OUTPUT_CLAIMS = (
    "see the response body",
    "shows the response body",
    "show the response body",
    "prints the response body",
    "prints the body",
    "print the body",
    "the body is printed",
)


def test_the_documented_probe_is_not_described_as_printing_what_it_asserts() -> None:
    section = _container_docs()
    documented = EXEC_PROBE.search(section)
    assert documented is not None, (
        "docs/adapters.md must show the on-demand probe verbatim, not a paraphrase"
    )
    if "print(" in documented.group("script"):
        return
    text = _normalised_text(section)
    for claim in BODY_OUTPUT_CLAIMS:
        assert claim not in text, (
            f"the documented probe asserts on the body and never prints it, so the prose "
            f"cannot promise to {claim!r}. Either print the body (without the token) or say "
            "the probe is run on demand"
        )


def test_no_host_http_call_is_documented_for_the_example() -> None:
    for name, text in ((COMPOSE, _read(COMPOSE)), (DOCS, _container_docs())):
        match = HOST_HTTP_CALL.search(text)
        assert match is None, (
            f"{name} tells the operator to call the API from the host ({match.group(0).strip()!r}). "
            "The token lives in `.env`, which Compose interpolates and does not export, so the "
            "call would carry an empty token - and exporting it puts the value in that client's argv"
        )


def test_the_example_says_what_the_check_reports() -> None:
    text = _normalised_text(_container_docs())
    assert any(scope in text for scope in PROBE_SCOPES), (
        "say that `docker compose ps` reports the container's health status rather than "
        "making a call from the host, so the operator knows what was and was not exercised"
    )
