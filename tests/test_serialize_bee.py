#!/usr/bin/env python3
"""Unit tests for `_serialize_bee` — the packer's `.celaut.bee` writer.

`nodo import` reads a packed bee with
`read_from_file(indices={1: Metadata, 2: Service})` and consumes BOTH blocks, so
the packer must emit a two-index `[Metadata, Service]` bee (not service-only, which
made `import_bee` raise `StopIteration`). These tests stub the lazily-imported
`bee_rpc.client` + `protos.celaut_pb2` so they run with no nodo/Docker deps, and
assert the framing contract: metadata block first, service dir second, under
`{1: Metadata, 2: Service}` indices.

Run:  python -m unittest tests.test_serialize_bee   (from the repo root)
"""
import os
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager

# Point registry/cache dirs at throwaway temps BEFORE importing server (matches
# the sibling test module's module-level env contract).
_TMP = tempfile.mkdtemp(prefix="packer-bee-test-")
os.environ.setdefault("CACHE", os.path.join(_TMP, "cache"))
os.makedirs(os.environ["CACHE"], exist_ok=True)

import server  # noqa: E402


class _FakeMetadata:
    def SerializeToString(self):  # noqa: N802 - protobuf API shape
        return b""


class _FakeService:
    pass


class _FakeDir:
    """Stand-in for bee_rpc.client.Dir(dir=..., _type=...)."""

    def __init__(self, dir, _type):  # noqa: A002 - mirror the real kwarg name
        self.dir = dir
        self._type = _type


@contextmanager
def _stub_nodo_deps(recorder):
    """Inject fake `bee_rpc.client` + `protos.celaut_pb2` modules so
    `_serialize_bee`'s lazy `from ... import ...` resolves to our stubs."""
    def fake_write_to_file(path, file_name, extension, input, indices):
        blocks = list(input)  # materialise the generator
        recorder["blocks"] = blocks
        recorder["indices"] = indices
        # Capture on-disk block contents NOW — `_serialize_bee` rmtree's its
        # scratch dir on return, so the metadata file won't exist afterwards.
        recorder["block_contents"] = [
            open(b.dir, "rb").read() if os.path.isfile(b.dir) else None
            for b in blocks
        ]
        out = os.path.join(path, f"{file_name}.{extension}")
        with open(out, "wb") as f:
            f.write(b"FAKEBEE")
        recorder["out_path"] = out
        return out

    bee_pkg = types.ModuleType("bee_rpc")
    bee_client = types.ModuleType("bee_rpc.client")
    bee_client.write_to_file = fake_write_to_file
    bee_client.Dir = _FakeDir
    bee_pkg.client = bee_client

    protos_pkg = types.ModuleType("protos")
    celaut = types.ModuleType("protos.celaut_pb2")
    celaut.Metadata = _FakeMetadata
    celaut.Service = _FakeService
    protos_pkg.celaut_pb2 = celaut

    injected = {
        "bee_rpc": bee_pkg,
        "bee_rpc.client": bee_client,
        "protos": protos_pkg,
        "protos.celaut_pb2": celaut,
    }
    originals = {name: sys.modules.get(name) for name in injected}
    sys.modules.update(injected)
    try:
        yield recorder
    finally:
        for name, original in originals.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


class SerializeBeeTests(unittest.TestCase):
    def test_emits_two_index_metadata_then_service(self):
        rec = {}
        with _stub_nodo_deps(rec):
            out = server._serialize_bee("/some/service_dir", b"META-BYTES")

        # Returns the bytes write_to_file produced.
        self.assertEqual(out, b"FAKEBEE")

        # Framed as {1: Metadata, 2: Service}.
        self.assertEqual(rec["indices"], {1: _FakeMetadata, 2: _FakeService})

        # Two blocks, metadata first then the service dir.
        blocks = rec["blocks"]
        self.assertEqual(len(blocks), 2)
        self.assertIs(blocks[0]._type, _FakeMetadata)
        self.assertIs(blocks[1]._type, _FakeService)
        self.assertEqual(blocks[1].dir, "/some/service_dir")

        # Metadata bytes were materialised to the file the first block points at.
        self.assertEqual(rec["block_contents"][0], b"META-BYTES")

    def test_empty_metadata_falls_back_to_serialized_empty_metadata(self):
        rec = {}
        with _stub_nodo_deps(rec):
            server._serialize_bee("/svc", b"")
        # b"" is falsy -> an empty celaut.Metadata() is serialized in its place
        # (never a null metadata block).
        self.assertEqual(rec["block_contents"][0], _FakeMetadata().SerializeToString())

    def test_cleans_up_its_scratch_dir(self):
        rec = {}
        with _stub_nodo_deps(rec):
            server._serialize_bee("/svc", b"META")
        # _serialize_bee makes a mkdtemp under CACHE and removes it on the way out.
        self.assertFalse(os.path.exists(os.path.dirname(rec["out_path"])))


if __name__ == "__main__":
    unittest.main()
