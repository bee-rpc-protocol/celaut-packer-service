# Running the packer locally

This service packs a project (a `service.json` + `Dockerfile` + sources) into a
content-addressed `.celaut.bee`, exactly the way `nodo pack` does. It builds your
service's filesystem with `docker buildx` **inside its own container**
(docker-in-docker), so running it locally needs a privileged container.

## Requirements

- **Docker** on the host, able to run **`--privileged`** containers (the service
  boots its own `dockerd` + `buildx` builder inside the container).
- **x86_64 host** recommended. The image emulates `arm64` via qemu/binfmt, so
  packing arm64 services works, but native amd64 is the fast path.
- Outbound internet (buildx pulls the base images your `Dockerfile` references).
- ~4 GB free disk for the nested image cache.

No `config.yaml` is needed. The determinism-critical settings are hardcoded in
the source (`src/utils/hashing.py` → `sha3_256`, `src/packers/zip_with_dockerfile.py`
→ `MIN_BUFFER_BLOCK_SIZE = 10 MiB`); runtime dirs come from `start.sh` env.

## Build the image

```bash
docker build -t packer-service .
```

The build asserts the three baked-in source fixes are present (two determinism,
one buildx-abspath) — if any is missing, the build fails on purpose.

## Run it

The image has no `ENTRYPOINT` on purpose (nodo supplies it from `service.json`);
locally, run `start.sh` yourself. `--privileged` is required for the nested
`dockerd`.

```bash
docker run --rm --privileged \
  -p 8080:8080 \
  -p 8443:8443 \
  --name pkr packer-service /app/start.sh
```

Ports: `8080` = packer HTTP API, `8443` = browser VS Code IDE (optional).

Wait for `[packer] listening on 0.0.0.0:8080` in the logs. (First boot may log
`overlay2 not supported → vfs` depending on your kernel; that's fine — buildx
uses its own `nodo-hostnet` BuildKit builder.)

## Pack a service

`POST /pack` with the project as a zip (`application/zip`). A valid project has a
`service.json` and a `Dockerfile` at its root (or in a `.service/` folder). On
success you get `200`, the `.celaut.bee` bytes as the body, and the id in the
`X-Service-Id` response header. On failure you get `400` with a plain-text reason.

```bash
cd examples/hello
zip -r /tmp/hello.zip .
curl -sS -D - -o /tmp/hello.celaut.bee \
  --data-binary @/tmp/hello.zip \
  -H "Content-Type: application/zip" \
  http://localhost:8080/pack
# -> HTTP/1.1 200 OK ... X-Service-Id: <hex id>
```

Packing the same project twice yields the **same** `X-Service-Id` — that
reproducibility is the whole point.

### Browser IDE (optional)

Open <http://localhost:8443> for a full VS Code in the browser (code-server) with
the example templates and a `pack-service` helper on the terminal PATH.

## Run the tests

Pure-Python unit tests (no Docker needed — `bee_rpc` is stubbed):

```bash
python -m unittest tests.test_serialize_bee
python -m unittest tests.test_registry_endpoint
```

## Layout

- `src/` — the packer source shipped to `/opt/nodo` (outer `src/` mirrors the
  `/opt/nodo` payload; inner `src/` is the Python package: `src.packers`, `src.utils`).
- `server.py` — the HTTP wrapper (`/pack`, `/registry/<id>`).
- `start.sh` — boots dockerd + buildx and launches the packer server.
- `examples/hello` — a reproducible reference service used to smoke-test packing.
