# celaut-packer-service

A Celaut microVM service that **does the same thing `nodo pack` does**, exposed
over HTTP. Give it a project archive (a zip with a `Dockerfile`, a
`pack_config.json` and a `service.json`); it builds the image, exports the
filesystem, serialises a content-addressed `celaut.Service`, and returns the
packed service as a single **`.celaut.bee`** file.

It is a thin wrapper around the *real* packer worker
(`src/packers/zip_with_dockerfile.py`), **shipped in-repo** under
[`src/`](src/) — not a re-implementation — so the `service_id`
it emits is byte-identical to `nodo pack`. That tree is the import closure of the
packer worker of nodo's **`dev`** branch (the packer, its `service_json`,
`src.utils`, `src.identity`, `src.manager` modules and the `protos/` package); the
image does not clone nodo at build time, so this service is self-contained. Like
nodo's own packer it builds with a **BuildKit** daemon driven by `buildctl` (no
Docker engine inside the VM). The project layout it accepts is the one of nodo's
[`docs/PACKING.md`](https://github.com/celaut-project/nodo/blob/dev/docs/PACKING.md).

A browser IDE (code-server) used to live in this repo; it is a separate project now, and it can use this packer as a dependency.

## API

Bound on `0.0.0.0:8080` (the only api slot, declared in `<arch>/.service/service.json`).

| Method | Path      | Body                | Response |
|--------|-----------|---------------------|----------|
| GET    | `/health` | —                   | `200 ok` |
| POST   | `/pack`   | project `.zip`      | `200` → `application/octet-stream` (`.celaut.bee`); header `X-Service-Id: <hex>`; `Content-Disposition: attachment; filename="<id>.celaut.bee"` |

Errors: `415` (not a zip), `400` (config or packing error), `413` (too large),
`500` (unexpected).

**Config validation:** before building, the `.service` configuration is checked
and any problems are returned as a numbered, plain-English list (invalid JSON
with line/column, missing/wrong `architecture`, no entrypoint, malformed `api`
slots, the `dependencies_env`+array trap, …) pointing at nodo's [`docs/PACKING.md`](https://github.com/celaut-project/nodo/blob/dev/docs/PACKING.md).
A bad config gets an actionable message instead of a cryptic Docker build
failure. Example body:

```
Your .service configuration has 2 problems:

  1. "architecture" is required in service.json. Add "architecture": "linux/amd64". (docs/PACKING.md → architecture)
  2. no entrypoint declared — the service has nothing to run. Add "init": { "entry_path": ["app", "start.sh"] } ...

Fix and re-pack. Full field reference: docs/PACKING.md of nodo (URL).
```

The zip is either the `.service.zip` that `nodo pack` sends, or a project zipped as
it is. A project must contain, at its root **or** inside a single top-level folder,
its configuration at the root (`Dockerfile`, `service.json`, optionally
`pack_config.json`) or in a `.service/` folder (the recommended layout). The server
prepares it as nodo does before it builds: the project files go under `service/`,
the `./` sources of the `COPY` lines are rewritten to `service/...`, and
`include`, `ignore` and `.dockerignore` choose the files.

**Dependencies.** `nodo pack` puts local and git dependencies in the zip. It uploads a
dependency that is in its registry with `GET`/`POST /registry/<id>` (the
bundle: `service`, `metadata`, `blocks/<id>`), so this server keeps those two
endpoints. A packed service is a multiblock directory, or one file when it has no blocks.

### Example

```bash
# zip a project (the same layout nodo pack expects). examples/hello has one pack
# root for each architecture; use the one of the packer instance (or the other:
# the packer emulates it with qemu).
( cd examples/hello/amd64 && zip -qr - . ) > /tmp/hello.zip

# pack it via the running service
curl -sS -X POST --data-binary @/tmp/hello.zip \
     -H 'Content-Type: application/zip' \
     -D /tmp/headers.txt -o /tmp/hello.celaut.bee \
     http://<service-ip>:8080/pack

grep -i x-service-id /tmp/headers.txt   # the content-addressed id
```

## How it works (architecture)

```
POST /pack (zip)
   │
   ▼
server.py  ── unzip, validate service.json, prepare the context ──┐
   │                                                              │
   ▼                                                              │
nodo worker:  python -m src.packers.zip_with_dockerfile           │  in-VM buildkitd
   │  buildctl build --output type=tar  ──────────────────────────┘  (host network)
   │  → export rootfs → walk fs → build celaut.Service protobuf
   │  → multiblock dir + content hash = service_id
   ▼
bee_rpc.read_multiblock_directory(service_dir) → bytes  →  <id>.celaut.bee
```

`start.sh` starts a `buildkitd` in the guest (overlayfs, with the native
snapshotter as a fallback) on the socket where nodo's packer looks for it
(`/opt/nodo/buildkit/buildkitd.sock`), registers the qemu handler of the other
architecture, starts the HTTP server, and then reads the notices of
the node on `/dev/hvc0`: on `STOP` or `HIBERNATE` it stops the server and BuildKit and
exits at once, so the node does not wait (`notice_declaration` is the default one in
`service.json`). **This is why the service declares `network: tag=(*)`** —
BuildKit must reach arbitrary container registries to pull the base images
referenced by the Dockerfile being packed.

The service declares `uses_gateway`: it asks its own node, through the gateway, for
more RAM when a pack needs it (`src/manager/resources.py`, with the
`node_controller` library, in a child process because the library and nodo's
`protos/` each register their own `celaut.proto`).

### Packing the packer: one pack root per architecture

The packer itself is released for both architectures, with the layout of
`celaut-basics/demo-service` and `bitcoin-node`:

```
amd64/  arm64/           pack roots: nodo pack amd64 | nodo pack arm64
├── .service/            Dockerfile, service.json, pack_config.json, .dockerignore (real files)
└── src server.py start.sh  -> symlinks to the shared sources at the root
```

`nodo pack <dir>` reads only `<dir>/.service/`, copies `<dir>` following symlinks and takes
the architecture from its `service.json`. The two `.service/` differ only in
`"architecture"` (the Dockerfile pins every base image by the digest of its multi-arch
index, and its first lines say which variant it is). `examples/hello` has the same
layout.

### Multi-arch (amd64 + arm64 via qemu)

An instance packs services for **both `linux/amd64` and `linux/arm64`**. It builds its
own architecture natively. For the other one, `start.sh` registers the qemu
`binfmt_misc` handler (`binfmt --install`, from the pinned `tonistiigi/binfmt`
stage of the Dockerfile) so BuildKit can run the `RUN` lines under emulation;
`src/utils/arch_guard.py` (from nodo) checks that the handler is enabled before a
foreign build starts, and refuses it with the name of the missing handler if not. The
target architecture is taken from each service's `service.json` `"architecture"`
field; supported values are configured with `PACKER_ARCHES` (default
`linux/amd64,linux/arm64`) and qemu registration can be disabled with
`PACKER_ENABLE_QEMU=0`.

## Ports

| Port | Service |
|---|---|
| `8080` | Packer HTTP API (`/health`, `/pack`, `/registry/<id>`) |

## Bootstrap / dogfood test

The acceptance test from the brief is a two-step bootstrap:

1. **Pack the packer with the node.** On an amd64 + KVM node:
   `nodo pack amd64` (or `nodo pack arm64` on an arm64 node) → the packer gets a
   real service-id; `nodo execute` it to get a running instance with an IP.
2. **Pack another service with the packer.** Zip e.g. `examples/hello` (or
   `celaut-sat-solver`) and `POST /pack` it to the running packer instance. The
   returned `.celaut.bee` / `X-Service-Id` must match what `nodo pack` produces
   for the same project. `nodo import <id>.celaut.bee` should accept it.

## Validation status

Checked with the image built for `linux/arm64` and run as a privileged container (the
guest of the microVM is not available in CI):

- **BuildKit path.** `POST /pack` of `examples/hello/arm64` gives `200` with an
  `X-Service-Id`, and a second pack of the same input gives the **same** id.
- **Same id as `nodo pack`.** That id (`24e26750…`) is the one that `nodo pack
  examples/hello/arm64` gives (the installed nodo, commit `76dcac3b`). Without the `/sys`
  rule below it was not: the rootful BuildKit of this service exports the build time for
  `/sys`, and nodo's rootless one leaves 0. The two tars of `examples/hello` have 522
  entries and are the same except for `/sys`.
- **Cross-architecture.** `examples/hello/amd64`, packed on the arm64 instance, builds
  under the qemu handler that `start.sh` registers.
- **Configuration errors.** A `service.json` with a wrong `read_only_filesystem`, both
  `protocol` and `protocol_stack`, and a `network` entry without `prose` gets a numbered
  list of problems and not a build failure.
- **Registry endpoints.** `POST` then `GET /registry/<id>` of a one-file service, and a
  second `POST`, answer as documented.

Not checked here: other projects than `examples/hello` against `nodo pack`, the amd64
id against a `nodo pack` on an amd64 node, and a boot inside a cloud-hypervisor microVM (overlayfs, `/dev/hvc0` notices, hotplugged RAM).

The id depends on three fixes that are **baked into the packer source** under `src/`
(search for `DETERMINISM PATCH`); the Dockerfile asserts both are present so a regression
fails the build:

1. **sorted directory walk** — `recursive_parsing` iterates `os.listdir()`
   sorted, so branch order is stable across extractions.
2. **zeroed symlink mtime** — `filesystem_xattrs` hashes `mtime_ns = 0` for a
   symlink; `tarfile` otherwise reassigns symlink mtimes to wall-clock on every
   extract. (nodo has this one upstream now.)

3. **`/sys` at epoch 0** (`BUILDER_MOUNT_POINTS_AT_EPOCH`) — see above. This service runs
   BuildKit as root; nodo's builder is rootless.

protobuf must be the **pure-python** implementation on both legs (the `upb`
C backend has unstable map ordering across runs).

### Notes

- **Execution target.** Running it inside a sealed cloud-hypervisor microVM needs a
  guest kernel with overlayfs (or the native snapshotter is used), `binfmt_misc`, and
  egress for BuildKit to pull base images.
- The size above which a file is stored as a block
  (`packer.MIN_BUFFER_BLOCK_SIZE`, `MIN_BUFFER_BLOCK_SIZE` here, 32 KiB) does not
  change the service id in nodo `dev`; it only moves memory to disk.
