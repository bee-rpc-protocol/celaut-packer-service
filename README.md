# celaut-packer-service

A Celaut microVM service that **does the same thing `nodo pack` does**, exposed
over HTTP. Give it a project archive (a zip with a `Dockerfile`, a
`pack_config.json` and a `service.json`); it builds the image, exports the
filesystem, serialises a content-addressed `celaut.Service`, and returns the
packed service as a single **`.celaut.bee`** file.

It is a thin wrapper around the *real* nodo packer
([`src/packers/zip_with_dockerfile.py`](https://github.com/celaut-project/nodo/tree/stable/src/packers)),
vendored into the image — not a re-implementation — so the `service_id` it emits
is byte-identical to `nodo pack`.

Since **v0.2.0** the same microVM also serves a **browser VS Code** on `:8443`
(see [Browser IDE](#browser-ide-code-server)) so users can pick a language
template, edit, and pack a service end-to-end without any local toolchain.

## API

Bound on `0.0.0.0:8080` (declared in `service.json`).

| Method | Path      | Body                | Response |
|--------|-----------|---------------------|----------|
| GET    | `/health` | —                   | `200 ok` |
| POST   | `/pack`   | project `.zip`      | `200` → `application/octet-stream` (`.celaut.bee`); header `X-Service-Id: <hex>`; `Content-Disposition: attachment; filename="<id>.celaut.bee"` |

Errors: `415` (not a zip), `400` (config or packing error), `413` (too large),
`500` (unexpected).

**Config validation:** before building, the `.service` configuration is checked
and any problems are returned as a numbered, plain-English list (invalid JSON
with line/column, missing/wrong `architecture`, no entrypoint, malformed `api`
slots, the `dependencies_env`+array trap, …) pointing at `SERVICE_CONFIG_GUIDE.md`.
A bad config gets an actionable message instead of a cryptic Docker build
failure. Example body:

```
Your .service configuration has 2 problems:

  1. "architecture" is required in service.json. Add "architecture": "linux/amd64". (SERVICE_CONFIG_GUIDE.md → architecture)
  2. no entrypoint declared — the service has nothing to run. Add "init": { "entry_path": ["app", "start.sh"] } ...

Fix and re-pack. Full field reference: SERVICE_CONFIG_GUIDE.md.
```

The zip must contain, at its root **or** inside a single top-level folder:
`Dockerfile`, `service.json`, and optionally `pack_config.json`.

### Example

```bash
# zip a project (the same layout nodo pack expects)
( cd examples/hello && zip -r - . ) > /tmp/hello.zip

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
server.py  ── unzip, validate (service.json + Dockerfile) ──┐
   │                                                        │
   ▼                                                        │
nodo worker:  python -m src.packers.zip_with_dockerfile     │  in-VM dockerd
   │  docker buildx build --output type=tar  ───────────────┘  (docker-in-docker)
   │  → export rootfs → walk fs → build celaut.Service protobuf
   │  → multiblock dir + content hash = service_id
   ▼
bee_rpc.read_multiblock_directory(service_dir) → bytes  →  <id>.celaut.bee
```

`start.sh` boots an in-VM Docker daemon (DinD) and a host-network `buildx`
builder, then runs `server.py`. **This is why the service declares
`network: tag=(*)`** — buildx must reach arbitrary container registries to pull
the base images referenced by the Dockerfile being packed.

## Browser IDE (code-server)

The image also installs **code-server** (VS Code in the browser), served on the
second api slot `:8443`. `start.sh` seeds a `/workspace` and launches it
(`--auth none`; access is mediated by the nodo network/DNAT layer) before
handing the foreground to the packer server.

The workspace ships everything needed to build a Celaut service:

- **`START_HERE.md`** — the landing guide.
- **`SERVICE_CONFIG_GUIDE.md`** — the full upstream configuration reference
  ([zip_with_dockerfile README](https://github.com/celaut-project/nodo/blob/stable/src/commands/packer/zip_with_dockerfile/README.md)),
  always bundled at the workspace root and inside every service you create.
- **`templates/<lang>/`** — 8 preconfigured language templates, each with a
  ready `.service` config (`Dockerfile` + `service.json` + `pack_config.json`),
  a `start.sh`, and minimal buildable source:
  `python`, `go`, `rust`, `node-express`, `node-typescript`, `java-spring`,
  `java-gradle`, `php-laravel`.
- **`.vscode/tasks.json`** — a *New service from template* task that pops a
  language picker, plus a *Pack this service* task.

Two helper CLIs are on `PATH` in the IDE terminal:

| Command | What |
|---|---|
| `new-service <lang> [name]` | Scaffold a template into the workspace (and drop the config guide inside it). |
| `pack-service [dir]` | Zip a project and `POST` it to the local packer (`:8080`), saving `<service-id>.celaut.bee` next to it. |

So the full loop — **pick a template → edit → pack → get a `.celaut.bee`** —
happens inside one sealed microVM.

| Port | Service |
|---|---|
| `8080` | Packer HTTP API (`/health`, `/pack`) |
| `8443` | code-server (browser VS Code) |

## Bootstrap / dogfood test

The acceptance test from the brief is a two-step bootstrap:

1. **Pack the packer with the node.** On the amd64 node (Alienware WSL2):
   `nodo pack workspace/celaut-packer-service` → `celaut-packer-service` gets a
   real service-id; `nodo execute` it to get a running instance with an IP.
2. **Pack another service with the packer.** Zip e.g. `examples/hello` (or
   `celaut-sat-solver`) and `POST /pack` it to the running packer instance. The
   returned `.celaut.bee` / `X-Service-Id` must match what `nodo pack` produces
   for the same project. `nodo import <id>.celaut.bee` should accept it.

## Status & the one open risk

- Scaffold complete; `server.py` HTTP/zip layer is unit-tested locally.
- **End-to-end (build + real `.bee` + dogfood) can only run on an amd64 + KVM
  node** — this Mac is aarch64 with no KVM. That validation runs on Alienware.
- **Open risk:** whether a cloud-hypervisor microVM permits a nested Docker
  daemon (DinD needs privileged/overlay/iptables). If Celaut's runtime forbids
  it, the fix is localised to `start.sh` + `Dockerfile` — swap DinD for talking
  to the host nodo's dockerd socket — while `server.py` and the packing contract
  stay the same.
- The Python dep set in the `Dockerfile` (bee_rpc + nodo requirements) is the
  other spot that may need a pin tweak on the first real amd64 build.
