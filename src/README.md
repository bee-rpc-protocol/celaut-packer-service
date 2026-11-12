# src/ — the packer source (maintained here)

This directory carries the **packing/build code** for the service, so it is
**self-contained**: the image does not `git clone` nodo at build time. It comes from
[`celaut-project/nodo`](https://github.com/celaut-project/nodo), branch **`dev`**.

> Layout note: the outer `src/` mirrors the `/opt/nodo` payload (`COPY ./src/
> /opt/nodo/`); the inner `src/` is the Python package root, so the worker is
> importable as `src.packers.zip_with_dockerfile` with cwd `/opt/nodo`.

## What's here

The import closure of the packer worker `src.packers.zip_with_dockerfile` (invoked
as `python -m src.packers.zip_with_dockerfile --worker <zip> <result.json>` by
[`server.py`](../server.py)): the files that its module-level imports reach, plus the
ones that it imports inside functions for the network `formal` checks
(`src.identity.node_identity`, `src.manager.network_templates`,
`src.manager.pow_networks`). The closure was computed by static AST analysis and
checked by importing the worker (`python -c "import src.packers.zip_with_dockerfile"`).
`src/utils/block_tree.py` is not in it: `server.py` uses it for the registry endpoints.

```
protos/                          # the generated *_pb2 of celaut/pack/invite, gateway_bee (not the .proto sources)
src/packers/{zip_with_dockerfile,service_json}.py
src/utils/                       # hashing, filesystem_xattrs, benchmark, notice,
                                 # shared_filesystems, buildkit_env, arch_guard, ...
src/identity/, src/routing/, src/manager/   # imported by the network checks
```

## Files that are not copies of nodo

- `src/utils/config.py` — nodo's loads `config.yaml` and, on a first run, writes one (a
  wallet mnemonic, a gateway port). This one keeps the `ConfigManager().get(key,
  default)` interface and answers the keys that the packer reads from a table of
  defaults and the environment. A key it does not hold answers the `default` of the
  caller.
- `src/manager/resources.py` — nodo's, plus the growth of RAM through the node
  (`NodeControllerResourceManager`, which runs `node_controller` in a child process: its
  `celaut.proto` and nodo's cannot share one descriptor pool).

## Fixes baked into the source

Grep `DETERMINISM PATCH` and `BUILDER MOUNT POINT PATCH`. The Dockerfile asserts all three are present:

1. `src/packers/zip_with_dockerfile.py` — `recursive_parsing` iterates
   `sorted(os.listdir(...))`, so the order of the filesystem branches (and the
   serialized bytes / service id) does not depend on the extraction. *(determinism;
   not in nodo)*
2. `src/utils/filesystem_xattrs.py` — `mtime_ns = 0` for a symlink (tar gives it the time
   of the extraction). *(determinism; nodo has it)*
3. `src/packers/zip_with_dockerfile.py` — `/sys` is exported at epoch 0
   (`BUILDER_MOUNT_POINTS_AT_EPOCH`). The `RUN` steps see `/sys`, `/proc` and `/dev`
   mounted, so a Dockerfile's `touch` cannot reach them and the builder sets their mtime.
   The two tars of `examples/hello` (nodo's rootless BuildKit, and the rootful one of this
   service) have the same 522 entries except `/sys`: the rootful daemon names the build
   time, the rootless one leaves 0. nodo's id counts those mtimes, so `/sys` takes nodo's
   value. *(id parity with `nodo pack`)*

## Updating

To refresh against nodo, copy the files above from `dev` again (the closure can change:
recompute it), keep `config.py` and the RAM growth of `resources.py`, apply patches 1 and 3
again, and run the pack tests of [LOCAL.md](../LOCAL.md).
