# src/ — the packer source (maintained here)

This directory carries the **packing/build code** for the service, so it is
**self-contained**: the image no longer `git clone`s nodo at build time, and the
service keeps working even after nodo removes its own local Docker packer. The
code originally derived from [`celaut-project/nodo`](https://github.com/celaut-project/nodo)
but is now maintained here (fixes below should be upstreamed to nodo too).

> Layout note: the outer `src/` mirrors the `/opt/nodo` payload (`COPY ./src/
> /opt/nodo/`); the inner `src/` is the Python package root, so the worker is
> importable as `src.packers.zip_with_dockerfile` with cwd `/opt/nodo`.

## What's here

It is the exact **transitive import closure** of the packer worker
`src.packers.zip_with_dockerfile` (invoked as
`python -m src.packers.zip_with_dockerfile --worker <zip> <result.json>` by
[`server.py`](../../server.py)), nothing more:

```
protos/                       # whole package (generated *_pb2 use bare imports)
src/__init__.py
src/manager/{__init__,resources}.py
src/packers/{__init__,zip_with_dockerfile}.py
src/utils/{__init__,arch_guard,config,filesystem_xattrs,hashing,
           logger,network,runtime,singleton,verify}.py
```

The closure was computed by static AST analysis (following `from src.* import`,
`from protos import <submodule>`, and the generated code's bare
`import <name>_pb2`) and **verified** by importing
`src.packers.zip_with_dockerfile` against this tree with the external deps
present — it resolves every internal module and stops only at runtime.py's
expected "docker binary not found" check (satisfied in the image by the static
engine binaries layered under `/opt/nodo/bin`).

## Fixes baked into the source

Three fixes are applied **directly in this source** (grep `DETERMINISM PATCH`).
The Dockerfile asserts all three are present so a regression fails the build:

1. `src/packers/zip_with_dockerfile.py` — `recursive_parsing` iterates
   `sorted(os.listdir(...))` so filesystem branch order (and the serialized
   bytes / service-id) is stable across extractions of the same tar. *(determinism)*
2. `src/utils/filesystem_xattrs.py` — `metadata_from_lstat` hashes
   `mtime_ns = 0` (tar reassigns symlink mtimes to wall-clock time, which would
   otherwise change the id every pack). *(determinism)*
3. `src/packers/zip_with_dockerfile.py` — the `docker buildx build` runs with
   `cwd=self.path` (the build-context dir), but the `--output type=tar,dest=…`
   and the context arg were `CACHE`-relative, so under that cwd they double-nested:
   buildx couldn't find the context or write `filesystem.tar`, and every pack
   failed with HTTP 400. Both are now `os.path.abspath(...)`. *(correctness)*

## Identity code ported from nodo

The service id covers the filesystem metadata, so the packer has to read an image the way
`nodo pack` does. These parts of nodo's `parseFilesys` are ported here (the rest of nodo's
packer is not: nodo builds with a rootless BuildKit, this service with `docker buildx`):

- **Symlink targets** are kept as the image has them (`os.readlink`), not resolved with
  `os.path.realpath` (nodo #485: with `realpath` the id depended on the packing host).
  An escaping link stops the pack.
- **uid, gid, mode and mtime** come from the tar header (`metadata_from_tarinfo`), and a
  directory with no member in the tar gets `implicit_directory_metadata()`.
- **xattrs are written sorted by key**, as nodo writes its sorted list; the pure-python
  protobuf serializes a map in insertion order, so the bytes are the same.

- **`/sys` is exported at epoch 0** (`BUILDER_MOUNT_POINTS_AT_EPOCH`). The `RUN` steps see
  `/sys`, `/proc` and `/dev` mounted, so a Dockerfile's `touch` cannot reach them and the
  builder sets their mtime. Docker's export and nodo's rootless BuildKit export of
  `examples/hello` (522 entries each) are the same except `/sys`: Docker names the build time
  and BuildKit leaves 0. `/proc`, `/dev` and `/etc/hosts` are the same in both. nodo's id
  counts those mtimes, so `/sys` takes nodo's value.

Checked on `examples/hello` (`linux/arm64`): the id that `POST /pack` gives is the one that
`nodo pack` gives for the same directory.

## Updating

To refresh against upstream nodo, re-copy the files listed above from the nodo
`stable` branch and re-apply the two determinism patches (or keep them — they
are content-irrelevant normalisations worth upstreaming).
