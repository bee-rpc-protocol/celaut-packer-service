# Running the packer locally

This service packs a project (a `service.json` + `Dockerfile` + sources) into a
content-addressed `.celaut.bee`, exactly the way `nodo pack` does. It builds your
service's filesystem with a **BuildKit** daemon (`buildkitd` + `buildctl`, as nodo's
own packer does) **inside its own container**, so running it locally needs a
privileged container.

## Requirements

- **Docker** on the host, able to run **`--privileged`** containers (the service
  starts its own `buildkitd` inside the container).
- Build the image for the architecture of the host (`amd64/` or `arm64/`). The
  instance packs both: the other architecture runs under qemu (the handler is
  registered in the kernel of the host while the container runs), and the native one
  is the fast path.
- Outbound internet (BuildKit pulls the base images your `Dockerfile` references).
- ~4 GB free disk for the BuildKit cache.

No `config.yaml` is needed: `src/utils/config.py` answers the few keys that the packer
reads from a table of defaults (hash `sha3_256`, `MIN_BUFFER_BLOCK_SIZE` 32 KiB, which
does not change the id), and the directories come from the `start.sh` env.

## Build the image

```bash
# a pack root holds symlinks, which Docker does not follow out of its context:
# copy it with the links resolved (use arm64 on an arm64 host)
cp -rL amd64 /tmp/packer-ctx
docker build -t packer-service -f /tmp/packer-ctx/.service/Dockerfile /tmp/packer-ctx
```

The build asserts the two baked-in determinism fixes are present, and that the packer
and `node_controller` import — if one fails, the build fails on purpose.

## Run it

The image has no `ENTRYPOINT` on purpose (nodo supplies it from `service.json`);
locally, run `start.sh` yourself. `--privileged` is required for `buildkitd` and for
the qemu handler. Give the BuildKit data directory a **volume**: overlayfs does not
work on top of the overlayfs of the container filesystem, and a `RUN` line would fail
with `mount source: "overlay"`. (In the microVM the root filesystem is not an overlay.)

```bash
docker run --rm --privileged \
  -p 8080:8080 \
  -v packer-buildkit:/opt/nodo/buildkit/data \
  --name pkr --entrypoint /bin/bash packer-service /app/start.sh
```

Port: `8080` = packer HTTP API.

Wait for `[packer] listening on 0.0.0.0:8080` in the logs. (If the kernel has no
overlayfs, `start.sh` logs that `buildkitd` did not start and retries with the native
snapshotter.)

## Pack a service

`POST /pack` with the project as a zip (`application/zip`). A valid project has a
`service.json` and a `Dockerfile` at its root (or in a `.service/` folder). On
success you get `200`, the `.celaut.bee` bytes as the body, and the id in the
`X-Service-Id` response header. On failure you get `400` with a plain-text reason.

```bash
cd examples/hello/amd64      # or arm64: one pack root for each architecture
zip -qr /tmp/hello.zip .
curl -sS -D - -o /tmp/hello.celaut.bee \
  --data-binary @/tmp/hello.zip \
  -H "Content-Type: application/zip" \
  http://localhost:8080/pack
# -> HTTP/1.1 200 OK ... X-Service-Id: <hex id>
```

Packing the same project twice yields the **same** `X-Service-Id` — that
reproducibility is the whole point.

## To pack the self repo

```bash
cp -rL amd64 /tmp/packer-root   # or arm64
(cd /tmp/packer-root && zip -r /tmp/packer.zip .)
curl -sS -D - -o /tmp/packer.celaut.bee \
  --data-binary @/tmp/packer.zip \
  -H "Content-Type: application/zip" \
  http://localhost:8080/pack
```

## Run the tests

Pure-Python unit tests (no Docker or BuildKit needed). They need the packer's Python
dependencies (`protobuf==4.23.3`, `bee_rpc` v0.0.3, `psutil`, `mnemonic`, `cryptography`,
`requests`):

```bash
python -m unittest discover -s tests
```

## Layout

- `src/` — the packer source shipped to `/opt/nodo` (outer `src/` mirrors the
  `/opt/nodo` payload; inner `src/` is the Python package: `src.packers`, `src.utils`).
- `server.py` — the HTTP wrapper (`/pack`, `/registry/<id>`).
- `start.sh` — starts buildkitd, registers qemu, launches the packer
  server, and reads the notices of the node.
- `amd64/`, `arm64/` — the pack roots of this service (`.service/` + links to the sources).
- `examples/hello` — a reproducible reference service used to smoke-test packing (one
  pack root for each architecture).
