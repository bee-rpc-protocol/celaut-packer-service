#!/usr/bin/env python3
"""
Celaut Packer Service — HTTP front-end.

A network-facing Celaut microVM service that does *the same thing the nodo
packer does* (src/packers/zip_with_dockerfile.py): given a project archive
containing a Dockerfile, a packer config and a service.json, it builds the
image, exports its filesystem, serialises a content-addressed `celaut.Service`
protobuf, and returns the packed service as a single `.celaut.bee` file.

It does NOT re-implement the packer. It ships the packer source of nodo's `dev`
branch in-repo (src/, COPYed to /opt/nodo at image-build time — no clone of nodo)
and invokes its worker entrypoint, so the service-id produced here is
byte-identical to `nodo pack`. BuildKit runs
inside this microVM through a BuildKit daemon that the worker drives with
`buildctl`, as nodo's own packer does; that is why the service declares
`network: tag=(*)` — BuildKit must reach arbitrary registries to pull base images.

API (bound on 0.0.0.0:8080):
  GET  /health            -> 200 "ok"
  GET  /registry/<id>     -> 200 JSON {present, metadata, blocks} if the packed
                             dependency <id> is already in this packer's
                             filesystem registry, else 404 {present:false}. Lets
                             a client (nodo) skip re-uploading a dependency the
                             packer already holds.
  POST /registry/<id>     -> body = a gzip tar bundle of an already-packed
                             service (a "registry dependency"). Injected into the
                             packer host's REGISTRY / METADATA_REGISTRY / BLOCKDIR
                             dirs so a subsequent /pack whose pack_config declares
                             <id> as a registry dependency can resolve it (the
                             vendored ggconf reads {REGISTRY}/<id>). Idempotent.
                             Bundle layout (see _store_registry_bundle):
                               service/         -> the multiblock service dir
                                                   (becomes {REGISTRY}/<id>/)
                               metadata         -> optional Metadata protobuf
                                                   (becomes {METADATA_REGISTRY}/<id>)
                               blocks/<blockid> -> optional shared block files
                                                   (become {BLOCKDIR}/<blockid>)
                             200 -> JSON {service_id, stored, already_present,
                                    blocks_added}
                             400 -> malformed/missing id or bad bundle
  POST /pack              -> body = project .zip (application/zip | octet-stream)
                             200 -> application/octet-stream, the `.celaut.bee`
                                    headers: X-Service-Id: <hex>
                                             Content-Disposition: attachment;
                                                 filename="<service_id>.celaut.bee"
                             400 -> config or packing error (text body). The
                                    .service configuration is validated up front
                                    (valid JSON, architecture, entrypoint, api
                                    slots, pack_config) and any problems are
                                    returned as a numbered, plain-English list
                                    pointing at nodo's docs/PACKING.md, so a bad
                                    config never reaches a cryptic build failure.
                             415 -> bad/missing zip

The zip is either the `.service.zip` that `nodo pack` sends, or the project
itself, which is prepared as nodo prepares it (project files under service/, the
`./` COPY sources rewritten to `service/`, `include`/`ignore`/`.dockerignore`
applied). It must contain (at its root, or in a single top-level folder):
  - Dockerfile           (root or .service/)
  - service.json         (root or .service/)
  - pack_config.json     (optional)
"""
import base64
import json
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# src/utils/block_tree.py is pure stdlib: the blocks that a packed service needs.
# It lives in the packer tree (/opt/nodo in the image; the outer src/ in a checkout).
for _root in (os.environ.get("NODO_DIR", "/opt/nodo"),
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")):
    if os.path.isfile(os.path.join(_root, "src", "utils", "block_tree.py")):
        sys.path.insert(0, _root)
        break
from src.utils.block_tree import (  # noqa: E402
    InvalidBlockIdError, MissingManifestError, block_tree,
)

# Where the nodo source tree is vendored in the image (see Dockerfile).
NODO_DIR = os.environ.get("NODO_DIR", "/opt/nodo")
PORT = int(os.environ.get("PORT", "8080"))
MAX_ZIP_BYTES = int(os.environ.get("MAX_ZIP_BYTES", str(512 * 1024 * 1024)))  # 512MB
# This packer builds the architecture of its own guest natively, and the other one
# through the qemu/binfmt handler that start.sh registers (src/utils/arch_guard.py
# refuses a foreign build when the handler is missing).
PACKER_ARCH = os.environ.get(  # default suggested in errors
    "PACKER_ARCH",
    "linux/arm64" if platform.machine().lower() in ("aarch64", "arm64") else "linux/amd64",
)
SUPPORTED_ARCHES = {
    a.strip() for a in os.environ.get(
        "PACKER_ARCHES", "linux/amd64,linux/arm64").split(",") if a.strip()
}
GUIDE = "docs/PACKING.md of nodo (https://github.com/celaut-project/nodo/blob/dev/docs/PACKING.md)"

# The nodo ConfigManager reads these; start.sh exports them too. Kept here so the
# server is runnable/inspectable on its own.
os.environ.setdefault("CACHE", "/var/lib/celaut/cache/")
os.environ.setdefault("BLOCKDIR", "/var/lib/celaut/blocks/")

# A packed service's on-disk id: content hash (hex) or a tag. Keep it filesystem
# safe — no separators / traversal — since it names a directory under REGISTRY.
_SERVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,255}$")

# Cache of the packer's registry dirs so we only resolve them once.
_REGISTRY_DIRS = None


def _registry_dirs():
    """Resolve (REGISTRY, METADATA_REGISTRY, BLOCKDIR) — the dirs a registry
    dependency is resolved against. These MUST match what the packer worker
    uses, otherwise an injected dependency lands where the packer can't see it.

    There is no config.yaml (this is a single-purpose service, not a node): the
    dirs come from explicit PACKER_*_DIR env overrides (used by tests to point at
    temp dirs), then BLOCKDIR from start.sh env, then the image's built-in
    defaults under /opt/nodo/storage.
    """
    global _REGISTRY_DIRS
    if _REGISTRY_DIRS is not None:
        return _REGISTRY_DIRS
    registry = (os.environ.get("PACKER_REGISTRY_DIR")
                or "/opt/nodo/storage/__registry__/")
    metadata = (os.environ.get("PACKER_METADATA_DIR")
                or "/opt/nodo/storage/__metadata__/")
    blocks = (os.environ.get("PACKER_BLOCKS_DIR")
              or os.environ.get("BLOCKDIR")
              or "/opt/nodo/storage/__block__/")
    _REGISTRY_DIRS = (registry, metadata, blocks)
    return _REGISTRY_DIRS


def _registry_status(service_id: str):
    """Return a dict describing whether <service_id> is present in the packer's
    registry (and, if so, whether metadata + which blocks are there), or None if
    the service isn't present at all.

    A packed service is a multiblock directory (its `_.json` names the blocks), or
    one file when it has no blocks. The blocks listed are all of those that the
    service needs, at every depth (a block can be a multiblock directory too)."""
    registry, metadata, blockdir = _registry_dirs()
    service_path = os.path.join(registry, service_id)
    if not os.path.exists(service_path):
        return None
    blocks = []
    if os.path.isdir(service_path):
        try:
            blocks = block_tree(service_path, blockdir)
        except (OSError, ValueError, MissingManifestError, InvalidBlockIdError):
            blocks = []
    return {
        "present": True,
        "metadata": os.path.exists(os.path.join(metadata, service_id)),
        "blocks": blocks,
    }


def _safe_extract_tar(tf: tarfile.TarFile, dest: str):
    """Extract a tar guarding against path traversal (absolute paths, .., and
    symlinks/links escaping dest). Mirrors the zip handling used for /pack."""
    dest_abs = os.path.abspath(dest)
    for member in tf.getmembers():
        if member.issym() or member.islnk():
            raise RuntimeError(f"unsafe link member in bundle: {member.name!r}")
        target = os.path.abspath(os.path.join(dest, member.name))
        if target != dest_abs and not target.startswith(dest_abs + os.sep):
            raise RuntimeError(f"unsafe path in bundle: {member.name!r}")
    tf.extractall(dest)


def _store_registry_bundle(service_id: str, bundle_path: str) -> dict:
    """Inject an already-packed dependency into the packer host's registry so a
    later /pack can resolve `service_id` as a registry dependency.

    `bundle_path` is a gzip tar with this layout (all paths relative to root):
        service           -> the packed service: a multiblock dir (must contain
                             _.json), or one file when it has no blocks. It becomes
                             {REGISTRY}/{service_id}
        metadata          -> optional; the Metadata protobuf, copied verbatim to
                             {METADATA_REGISTRY}/{service_id}
        blocks/<blockid>  -> optional; content-addressed shared block files, each
                             copied to {BLOCKDIR}/<blockid> if not already present

    Content-addressed and therefore idempotent: if the service dir already
    exists we treat it as already-present and only backfill any missing blocks.
    Returns {service_id, stored, already_present, blocks_added}.
    """
    registry, metadata, blockdir = _registry_dirs()
    for d in (registry, metadata, blockdir):
        os.makedirs(d, exist_ok=True)

    extract = tempfile.mkdtemp(prefix="reg-", dir=os.environ["CACHE"])
    try:
        with tarfile.open(bundle_path, "r:*") as tf:
            _safe_extract_tar(tf, extract)

        src_service = os.path.join(extract, "service")
        if os.path.isdir(src_service):
            if not os.path.exists(os.path.join(src_service, "_.json")):
                raise RuntimeError(
                    "bundle 'service/' has no _.json manifest — not a packed service "
                    "directory.")
        elif not os.path.isfile(src_service):
            raise RuntimeError(
                "bundle is missing a top-level 'service' entry (the packed "
                "service: a multiblock directory, or one file when it has no "
                "blocks).")

        service_dir = os.path.join(registry, service_id)
        already_present = os.path.exists(service_dir)
        if not already_present:
            # Atomic-ish publish: move the fully-extracted service into place.
            shutil.move(src_service, service_dir)

        # Metadata (a single file) — write if provided and not already there.
        src_meta = os.path.join(extract, "metadata")
        if os.path.isfile(src_meta):
            dst_meta = os.path.join(metadata, service_id)
            if not os.path.exists(dst_meta):
                shutil.move(src_meta, dst_meta)

        # Blocks are shared + content-addressed: only add the ones we don't have.
        blocks_added = 0
        src_blocks = os.path.join(extract, "blocks")
        if os.path.isdir(src_blocks):
            for name in os.listdir(src_blocks):
                if not _SERVICE_ID_RE.match(name):
                    continue  # ignore anything with a suspicious name
                dst_block = os.path.join(blockdir, name)
                if not os.path.exists(dst_block):
                    shutil.move(os.path.join(src_blocks, name), dst_block)
                    blocks_added += 1

        return {
            "service_id": service_id,
            "stored": True,
            "already_present": already_present,
            "blocks_added": blocks_added,
        }
    finally:
        shutil.rmtree(extract, ignore_errors=True)


def _has_config(project: str, name: str) -> bool:
    """A required config file (service.json / Dockerfile) may sit at the project
    root (Option 2) or under .service/ (Option 1, the recommended layout). The
    vendored nodo packer's prepare_directory honours both, so accept either."""
    return os.path.exists(os.path.join(project, name)) or os.path.exists(
        os.path.join(project, ".service", name)
    )


def _locate_config(project: str, name: str):
    """Return the path the packer will actually use for a config file. nodo's
    prepare_directory prefers .service/ when that dir exists, else the project
    root. Returns None if the file is in neither place."""
    svc = os.path.join(project, ".service", name)
    root = os.path.join(project, name)
    if os.path.isdir(os.path.join(project, ".service")) and os.path.exists(svc):
        return svc
    if os.path.exists(root):
        return root
    if os.path.exists(svc):
        return svc
    return None


def _load_json(path: str):
    """Return (obj, None) on success, or (None, "human-readable error") if the
    file isn't valid JSON. Reports the exact line/column of a syntax error."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except json.JSONDecodeError as e:
        return None, f"{e.msg} (line {e.lineno}, column {e.colno})"
    except OSError as e:
        return None, str(e)


def _validate_service_config(project: str):
    """Pre-flight check of the .service configuration. Returns a list of
    plain-English problems ("what's wrong + how to fix"). An empty list means the
    config is structurally sound (the build may still fail for other
    reasons, which are surfaced separately with the build log)."""
    problems = []

    sj_path = _locate_config(project, "service.json")
    if not sj_path:  # caller already guards this, but stay defensive
        return ["service.json is missing (expected at the project root or in a "
                ".service/ folder)."]

    sj, err = _load_json(sj_path)
    if err:
        return [f"service.json is not valid JSON — {err}. Common causes: a "
                "trailing comma, a missing comma, or unquoted keys/strings. Fix "
                "the syntax and re-pack."]
    if not isinstance(sj, dict):
        return [f"service.json must be a JSON object {{ ... }}, not a "
                f"{type(sj).__name__}."]

    # architecture — required. Builds amd64 natively and arm64 via qemu/binfmt.
    arch = sj.get("architecture")
    supported = ", ".join(sorted(SUPPORTED_ARCHES))
    if not arch:
        problems.append(
            f'"architecture" is required in service.json. Add e.g. '
            f'"architecture": "{PACKER_ARCH}" (one of: {supported}). '
            f'({GUIDE} → architecture)')
    elif not isinstance(arch, str):
        problems.append(f'"architecture" must be a string like "{PACKER_ARCH}", '
                        f'not {type(arch).__name__}.')
    elif arch not in SUPPORTED_ARCHES:
        problems.append(
            f'architecture "{arch}" can\'t be built by this packer. '
            f'Supported: {supported}.')

    # entrypoint — init.entry_path (preferred) or the legacy "entrypoint" field.
    init = sj.get("init")
    entry = init.get("entry_path") if isinstance(init, dict) else None
    if not entry and not sj.get("entrypoint"):
        problems.append(
            'no entrypoint declared — the service has nothing to run. Add '
            '"init": { "entry_path": ["app", "start.sh"] } pointing at the '
            f'executable/script inside your image. ({GUIDE} → init)')
    elif entry is not None and not isinstance(entry, (str, list)):
        problems.append('init.entry_path must be a string ("app/start.sh") or an '
                        'array of segments (["app", "start.sh"]).')
    else:
        # The node refuses to run an entry path with whitespace, "." or ".." (the
        # entry path is a path only: put the arguments in your entrypoint script).
        segments = entry if isinstance(entry, list) else str(entry or "").split("/")
        if any(not isinstance(x, str) for x in segments):
            problems.append('init.entry_path segments must be strings.')
        elif any(re.search(r"\s", x) for x in segments):
            problems.append('init.entry_path must be a single path: no spaces and '
                            'no CLI arguments. Put the flags inside your '
                            f'entrypoint script. ({GUIDE} → init)')
        elif any(x in (".", "..") for x in segments):
            problems.append('init.entry_path must not contain "." or ".." segments.')

    # tag — a string or an array of strings.
    tag = sj.get("tag")
    if tag is not None and not (
        isinstance(tag, str)
        or (isinstance(tag, list) and all(isinstance(t, str) for t in tag))
    ):
        problems.append('"tag" must be a string or an array of strings.')

    # api slots — optional, but validate shape when present. A slot speaks one
    # application "protocol" or a "protocol_stack" of several layers; both are
    # optional, and a slot does not declare the two.
    api = sj.get("api")
    if api is not None:
        if not isinstance(api, list):
            problems.append('"api" must be an array of slot objects, e.g. '
                            '[ { "port": 8080, "protocol": ["http"] } ].')
        else:
            for i, slot in enumerate(api):
                if not isinstance(slot, dict):
                    problems.append(f'api[{i}] must be an object with at least a '
                                    '"port".')
                    continue
                port = slot.get("port")
                if not isinstance(port, int) or isinstance(port, bool):
                    problems.append(f'api[{i}].port must be an integer (got '
                                    f'{port!r}).')
                elif not 1 <= port <= 65535:
                    problems.append(f'api[{i}].port {port} is out of range '
                                    '(1–65535).')
                if "protocol" in slot and "protocol_stack" in slot:
                    problems.append(f'api[{i}] declares both "protocol" and '
                                    '"protocol_stack": use one ("protocol" is a '
                                    f'single layer). ({GUIDE} → api)')
                if "protocol_stack" in slot and not isinstance(
                        slot["protocol_stack"], list):
                    problems.append(f'api[{i}].protocol_stack must be an array of '
                                    'layers, e.g. [["http2"], ["grpc"]].')
                mu = slot.get("mu_per_call")
                if mu is not None and not (
                    isinstance(mu, dict)
                    and all(isinstance(v, int) and not isinstance(v, bool)
                            for v in mu.values())
                ):
                    problems.append(f'api[{i}].mu_per_call must be an object of '
                                    'method name → integer price.')

    # network — optional. Every entry needs "tags" and "prose".
    network = sj.get("network")
    if network is not None:
        if not isinstance(network, list):
            problems.append('"network" must be an array of { "tags": [...], '
                            '"prose": "..." } entries.')
        else:
            for i, entry_net in enumerate(network):
                if not isinstance(entry_net, dict):
                    problems.append(f'network[{i}] must be an object with "tags" '
                                    'and "prose".')
                    continue
                for key in ("tags", "prose"):
                    if key not in entry_net:
                        problems.append(f'network[{i}] has no "{key}" — both '
                                        '"tags" and "prose" are required.')
                if "formal" in entry_net and not (
                    isinstance(entry_net["formal"], dict)
                    and all(isinstance(v, str) for v in entry_net["formal"].values())
                ):
                    problems.append(f'network[{i}].formal must be an object of '
                                    'string → string (a number is refused: write '
                                    'it as a JSON string).')

    # envs — names only.
    envs = sj.get("envs")
    if envs is not None and not (
        isinstance(envs, list) and all(isinstance(e, str) for e in envs)
    ):
        problems.append('"envs" must be an array of environment variable NAMES, '
                        'e.g. ["MY_ENV_VAR"].')

    # Booleans: a string "true" is an error, not a yes (the packer refuses it too).
    for key in ("read_only_filesystem", "uses_gateway"):
        if key in sj and not isinstance(sj[key], bool):
            problems.append(f'"{key}" must be the JSON boolean true or false, not '
                            f'{sj[key]!r}.')

    # notice_declaration — an object, or false for "no notice".
    notice = sj.get("notice_declaration")
    if notice is not None and notice is not False and not isinstance(notice, dict):
        problems.append('"notice_declaration" must be an object, or false when the '
                        f'service does not read /dev/hvc0. ({GUIDE} → notice_declaration)')

    # shared_filesystems — a list of objects; the packer checks each one.
    shared = sj.get("shared_filesystems")
    if shared is not None and not (
        isinstance(shared, list) and all(isinstance(x, dict) for x in shared)
    ):
        problems.append('"shared_filesystems" must be an array of objects with '
                        '"path" and "role".')

    # resources — at_init / at_most; the per-core benchmark is only an at_init key.
    resources = sj.get("resources")
    if resources is not None:
        if not isinstance(resources, dict):
            problems.append('"resources" must be an object with "at_init" and '
                            '"at_most".')
        else:
            for end in ("at_init", "at_most"):
                block = resources.get(end)
                if block is not None and not isinstance(block, dict):
                    problems.append(f'resources.{end} must be an object.')
            at_most = resources.get("at_most")
            if isinstance(at_most, dict) and "benchmark" in at_most:
                problems.append('resources.at_most.benchmark has no meaning: a '
                                'per-core minimum belongs in '
                                'resources.at_init.benchmark.')

    # Descendant workloads — arrays of clauses (the packer checks the shape).
    for key in ("initial_environment_workload", "possible_environment_workload"):
        if sj.get(key) is not None and not isinstance(sj[key], list):
            problems.append(f'"{key}" must be an array of {{ "any_of": [...] }} '
                            f'clauses. ({GUIDE} → {key})')

    # pack_config.json — optional; validate JSON + the dependencies_env/array trap.
    pc_path = _locate_config(project, "pack_config.json")
    if pc_path:
        pc, perr = _load_json(pc_path)
        if perr:
            problems.append(f"pack_config.json is not valid JSON — {perr}.")
        elif isinstance(pc, dict) and pc.get("dependencies_env") and isinstance(
            pc.get("dependencies"), list
        ):
            problems.append(
                '"dependencies_env": true requires "dependencies" to be an object '
                '(NAME → path), not an array, so each dependency gets an env-var '
                f'name. ({GUIDE} → dependencies_env)')

    return problems


def _rewrite_copy_sources(dockerfile: str) -> None:
    """The COPY rewrite of nodo's prepare_directory. The build context is the
    directory that holds `service/` (the project files), so every context source of
    a COPY that starts with "." gets the `service/` prefix (`./src` -> `service/src`).
    A COPY with `--from=<stage>` names a build stage, not the context: untouched."""
    with open(dockerfile, "r") as f:
        lines = f.readlines()
    with open(dockerfile, "w") as f:
        for line in lines:
            if line.strip().startswith("COPY "):
                parts = line.split()
                flags = [x for x in parts[1:] if x.startswith("--")]
                if len(parts) >= 3 and not any(x.startswith("--from") for x in flags):
                    start = 1
                    while start < len(parts) and parts[start].startswith("--"):
                        start += 1
                    for j in range(start, len(parts) - 1):  # the sources, not the dest
                        if parts[j].startswith("."):
                            parts[j] = "service" + parts[j][1:]
                    line = " ".join(parts) + "\n"
            f.write(line)


def _is_prepared_context(project: str) -> bool:
    """True when the zip is what `nodo pack` sends (its `.service.zip`): the
    Dockerfile and service.json at the root and the project files in `service/`.
    Nothing to prepare then."""
    return (
        not os.path.isdir(os.path.join(project, ".service"))
        and os.path.isdir(os.path.join(project, "service"))
        and os.path.exists(os.path.join(project, "Dockerfile"))
        and os.path.exists(os.path.join(project, "service.json"))
    )


def _stage_context(project: str, dest: str) -> str:
    """Prepare a project zipped as it is, the way `nodo pack` does before it sends
    it (prepare_directory + generate_service_zip), so that a Dockerfile builds here
    as it builds there and the service id is the same.

    The context is a directory with `Dockerfile`, `service.json` and
    `pack_config.json` at its root and the project files under `service/`:
      * the configuration comes from `.service/` or from the project root;
      * `include` (only these paths) and `ignore` (patterns, matched at every depth,
        plus the lines of `.dockerignore`) choose the project files;
      * the COPY sources that start with "." get the `service/` prefix.
    A zip that is already prepared (see _is_prepared_context) is used as it is.
    Returns the context directory.
    """
    if _is_prepared_context(project):
        return project
    svc = os.path.join(project, ".service")

    def config_path(name):
        for base in (svc, project):
            candidate = os.path.join(base, name)
            if os.path.exists(candidate):
                return candidate
        return None

    pack_config = {}
    pc_path = config_path("pack_config.json")
    if pc_path:
        loaded, _err = _load_json(pc_path)
        pack_config = loaded if isinstance(loaded, dict) else {}

    files_dir = os.path.join(dest, "service")
    os.makedirs(files_dir, exist_ok=True)
    include = pack_config.get("include")
    names = include if isinstance(include, list) else [
        n for n in os.listdir(project) if n != ".service"]
    for name in names:
        rel = str(name).strip("/")
        src = os.path.join(project, rel)
        # `include` can list ".service/pack_config.json": it is not project content.
        if not rel or rel.startswith(".service") or not os.path.exists(src):
            continue
        target = os.path.join(files_dir, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.isdir(src):
            shutil.copytree(src, target, dirs_exist_ok=True)
        else:
            shutil.copy2(src, target)

    patterns = list(pack_config.get("ignore") or [])
    ignore_file = config_path(".dockerignore")
    if ignore_file:
        with open(ignore_file, encoding="utf-8") as f:
            patterns += f.read().splitlines()
    root = pathlib.Path(files_dir)
    for pattern in patterns:
        pattern = str(pattern).strip()
        if not pattern or pattern.startswith(("#", "!")):
            continue
        pattern = pattern.rstrip("/")
        if not pattern:
            continue
        for found in root.rglob(pattern):
            if found.is_dir():
                shutil.rmtree(found, ignore_errors=True)
            else:
                found.unlink(missing_ok=True)

    for name in ("Dockerfile", "service.json", "pack_config.json"):
        src = config_path(name)
        if src:
            shutil.copy2(src, os.path.join(dest, name))
    _rewrite_copy_sources(os.path.join(dest, "Dockerfile"))
    return dest.rstrip("/") + "/"


def _flatten_single_root(extract_dir: str) -> str:
    """If the zip wrapped everything in one top-level folder, descend into it so
    the packer finds service.json/Dockerfile at `path`. Mirrors how `nodo pack`
    treats a project directory."""
    entries = [e for e in os.listdir(extract_dir) if not e.startswith("__MACOSX")]
    if len(entries) == 1:
        only = os.path.join(extract_dir, entries[0])
        if os.path.isdir(only) and _has_config(only, "service.json"):
            return only + "/"
    return extract_dir.rstrip("/") + "/"


def _run_packer(zip_path: str):
    """Invoke the real nodo packer worker out-of-process (exactly as nodo's
    pack_zip does) and return (service_id, metadata_bytes, service_dir) or raise.

    The worker writes a JSON result file: {service_id, metadata_b64, service_dir}
    or {error}.
    """
    result_path = os.path.join(
        os.environ["CACHE"], f"pack_result_{uuid.uuid4().hex}.json"
    )
    os.makedirs(os.path.dirname(result_path), exist_ok=True)
    cmd = [
        sys.executable, "-m", "src.packers.zip_with_dockerfile",
        "--worker", zip_path, result_path,
    ]
    proc = subprocess.run(cmd, cwd=NODO_DIR)
    if proc.returncode != 0:
        raise RuntimeError(f"packer worker exited {proc.returncode}")
    if not os.path.exists(result_path):
        raise RuntimeError("packer worker produced no result file")
    with open(result_path) as f:
        result = json.load(f)
    os.remove(result_path)
    if result.get("error"):
        raise RuntimeError(result["error"])
    metadata_b64 = result.get("metadata_b64")
    return (
        result["service_id"],
        base64.b64decode(metadata_b64) if metadata_b64 else b"",
        result["service_dir"],
    )


def _serialize_bee(service_dir: str, metadata_bytes: bytes) -> bytes:
    """Serialise the packed service as a two-index `[Metadata, Service]`
    `.celaut.bee` — the exact framing `nodo import` expects.

    nodo's `import_bee` reads the stream with
    `read_from_file(indices={1: Metadata, 2: Service})` and unconditionally
    consumes BOTH blocks (`next(it)` twice), so a service-only bee makes it raise
    `StopIteration` and abort the whole pack/import. We mirror nodo's own
    `export_bee` writer: hand `write_to_file` a generator that yields the metadata
    block first, then the service directory, under the same
    `{1: Metadata, 2: Service}` indices.

    `metadata_bytes` is the serialised `celaut.Metadata` the packer already
    produced in `ZipContainerPacker.save()`; its `hashtag` carries the service-id
    hashes, so the imported metadata block is complete and self-validating."""
    # Imported lazily so the module is importable without the nodo deps present
    # (e.g. for unit-testing the HTTP layer on a dev box).
    sys.path.insert(0, NODO_DIR)
    from bee_rpc.client import write_to_file, Dir  # type: ignore
    from bee_rpc.utils import modify_env  # type: ignore
    from protos import celaut_pb2  # type: ignore

    # The packer runs in a child process.  bee_rpc keeps its cache/block roots
    # in process-local class attributes, so the worker's modify_env() call is
    # not inherited here.  Configure the writer explicitly; otherwise it uses
    # cwd/__block__/ and can emit block markers without being able to read the
    # corresponding payloads produced by the worker.
    cache_dir = os.path.abspath(os.environ["CACHE"]) + os.sep
    block_dir = os.path.abspath(os.environ["BLOCKDIR"]) + os.sep
    modify_env(cache_dir=cache_dir, block_dir=block_dir)

    work = tempfile.mkdtemp(prefix="bee-", dir=os.environ["CACHE"])
    try:
        # write_to_file references each block by path; materialise the metadata
        # bytes as a file so it can be a Dir block alongside the service dir.
        meta_path = os.path.join(work, "metadata")
        with open(meta_path, "wb") as f:
            f.write(metadata_bytes or celaut_pb2.Metadata().SerializeToString())

        def _blocks():
            yield Dir(dir=meta_path, _type=celaut_pb2.Metadata)
            yield Dir(dir=service_dir, _type=celaut_pb2.Service)

        out_file = write_to_file(
            path=work,
            file_name="service",
            extension="celaut.bee",
            input=_blocks(),
            indices={
                1: celaut_pb2.Metadata,
                2: celaut_pb2.Service,
            },
        )
        with open(out_file, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(work, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "celaut-packer/0.3"

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt, *args):  # quieter, single-line logs to stderr
        sys.stderr.write("[packer] " + (fmt % args) + "\n")

    def _registry_id_from_path(self):
        """Parse a validated <id> out of /registry/<id>, or None (and send the
        appropriate error) if the path/id is malformed."""
        rest = self.path.split("?", 1)[0].rstrip("/")
        prefix = "/registry/"
        if not rest.startswith(prefix):
            self._send(404, "not found")
            return None
        service_id = rest[len(prefix):]
        if not service_id or "/" in service_id or not _SERVICE_ID_RE.match(service_id):
            self._send(400, "invalid service id in /registry/<id>")
            return None
        return service_id

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", "/healthz", ""):
            return self._send(200, "ok")
        if self.path.split("?", 1)[0].rstrip("/").startswith("/registry/"):
            service_id = self._registry_id_from_path()
            if service_id is None:
                return
            status = _registry_status(service_id)
            if status is None:
                return self._send(
                    404,
                    json.dumps({"service_id": service_id, "present": False}),
                    ctype="application/json")
            return self._send(
                200,
                json.dumps({"service_id": service_id, **status}),
                ctype="application/json")
        return self._send(404, "not found")

    def do_POST(self):
        if self.path.split("?", 1)[0].rstrip("/").startswith("/registry/"):
            return self._handle_registry_upload()
        if self.path.rstrip("/") != "/pack":
            return self._send(404, "not found")
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return self._send(415, "empty body; POST a project .zip")
        if length > MAX_ZIP_BYTES:
            return self._send(413, f"zip too large (> {MAX_ZIP_BYTES} bytes)")

        work = tempfile.mkdtemp(prefix="pack-", dir=os.environ["CACHE"])
        zip_path = os.path.join(work, "input.zip")
        try:
            remaining = length
            with open(zip_path, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)

            if not zipfile.is_zipfile(zip_path):
                return self._send(415, "body is not a valid zip archive")

            extract_dir = os.path.join(work, "src")
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(extract_dir)
            project = _flatten_single_root(extract_dir)
            if not _has_config(project, "service.json"):
                return self._send(400,
                    "No service.json found. Every service needs a service.json "
                    "(at the project root or in a .service/ folder) declaring its "
                    f"architecture, entrypoint, ports and network. See {GUIDE}.")
            if not _has_config(project, "Dockerfile"):
                return self._send(400,
                    "No Dockerfile found. The packer builds your service's "
                    "filesystem from a Dockerfile (at the project root or in a "
                    f".service/ folder). See {GUIDE} → Dockerfile.")

            # Pre-flight: explain any .service config mistakes before building, so
            # users get an actionable message instead of a cryptic build failure.
            problems = _validate_service_config(project)
            if problems:
                body = (
                    "Your .service configuration has "
                    f"{len(problems)} problem{'s' if len(problems) != 1 else ''}:\n\n"
                    + "\n".join(f"  {i}. {p}" for i, p in enumerate(problems, 1))
                    + f"\n\nFix and re-pack. Full field reference: {GUIDE}."
                )
                return self._send(400, body)

            # A project that is not what `nodo pack` sends gets prepared as nodo
            # prepares it: the context of the worker.
            project = _stage_context(project, os.path.join(work, "context"))

            # Re-zip the (possibly flattened) project for the worker, which unzips
            # to its own cache. Mirrors nodo's zipfile_ok contract.
            norm_zip = os.path.join(work, "project.zip")
            with zipfile.ZipFile(norm_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, _, files in os.walk(project):
                    for name in files:
                        ap = os.path.join(root, name)
                        zf.write(ap, os.path.relpath(ap, project))

            service_id, metadata_bytes, service_dir = _run_packer(norm_zip)
            bee = _serialize_bee(service_dir, metadata_bytes)

            fname = f"{service_id}.celaut.bee"
            return self._send(
                200, bee,
                ctype="application/octet-stream",
                headers={
                    "X-Service-Id": service_id,
                    "Content-Disposition": f'attachment; filename="{fname}"',
                },
            )
        except RuntimeError as e:
            return self._send(400,
                "Packing failed while building your service:\n\n"
                f"{e}\n\n"
                "This is usually a build error, not a config-shape problem. "
                "Common causes: a COPY referencing a file that isn't in the build "
                "context, a base image that can't be pulled, or a RUN step that "
                f"exits non-zero. See {GUIDE} → Common Issues.")
        except Exception as e:  # noqa: BLE001 - surface unexpected failures
            return self._send(500, f"internal error: {e}")
        finally:
            subprocess.run(["rm", "-rf", work], check=False)

    def _handle_registry_upload(self):
        """POST /registry/<id> — inject a packed dependency into the registry."""
        service_id = self._registry_id_from_path()
        if service_id is None:
            return
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return self._send(415, "empty body; POST a gzip tar dependency bundle")
        if length > MAX_ZIP_BYTES:
            return self._send(413, f"bundle too large (> {MAX_ZIP_BYTES} bytes)")

        os.makedirs(os.environ["CACHE"], exist_ok=True)
        work = tempfile.mkdtemp(prefix="reg-up-", dir=os.environ["CACHE"])
        bundle_path = os.path.join(work, "bundle.tar.gz")
        try:
            remaining = length
            with open(bundle_path, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)

            if not tarfile.is_tarfile(bundle_path):
                return self._send(415, "body is not a valid tar archive")

            result = _store_registry_bundle(service_id, bundle_path)
            return self._send(200, json.dumps(result), ctype="application/json")
        except RuntimeError as e:
            return self._send(400, f"invalid dependency bundle: {e}")
        except Exception as e:  # noqa: BLE001
            return self._send(500, f"internal error: {e}")
        finally:
            shutil.rmtree(work, ignore_errors=True)


def main():
    os.makedirs(os.environ["CACHE"], exist_ok=True)
    os.makedirs(os.environ["BLOCKDIR"], exist_ok=True)
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    sys.stderr.write(f"[packer] listening on 0.0.0.0:{PORT}\n")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
