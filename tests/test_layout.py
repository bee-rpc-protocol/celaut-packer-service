#!/usr/bin/env python3
"""The repo holds one pack root per architecture: `nodo pack amd64` / `nodo pack arm64`.

`nodo pack <dir>` reads `<dir>/.service/` and nothing else, takes the architecture from
`.service/service.json`, copies `<dir>` to its cache and follows symlinks. So each pack
root has its own `.service/` and reaches the shared sources through symlinks. This is the
layout of celaut-basics/demo-service. These tests check the shape without a node.

Run with:  python3 -m unittest tests.test_layout
"""
import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCHES = ("amd64", "arm64")
SERVICE_FILES = ("Dockerfile", "service.json", "pack_config.json", ".dockerignore")
SHARED = ("src", "ide", "server.py", "start.sh")


def _read(path):
    with open(path) as fh:
        return fh.read()


class PerArchitectureLayoutTests(unittest.TestCase):
    def test_no_service_files_at_the_root(self):
        for name in SERVICE_FILES + (".service",):
            self.assertFalse(os.path.exists(os.path.join(ROOT, name)), name)

    def test_each_pack_root_has_real_service_files(self):
        for arch in ARCHES:
            for name in SERVICE_FILES:
                path = os.path.join(ROOT, arch, ".service", name)
                self.assertTrue(os.path.isfile(path), f"{arch}: {name}")
                self.assertFalse(os.path.islink(path), f"{arch}: {name} is a symlink")

    def test_each_manifest_declares_its_directory_architecture(self):
        for arch in ARCHES:
            declared = json.loads(_read(os.path.join(ROOT, arch, ".service", "service.json")))
            self.assertEqual(declared["architecture"], f"linux/{arch}")

    def test_the_manifests_differ_only_in_the_architecture(self):
        manifests = []
        for arch in ARCHES:
            manifest = json.loads(_read(os.path.join(ROOT, arch, ".service", "service.json")))
            manifest.pop("architecture")
            manifests.append(manifest)
        self.assertEqual(manifests[0], manifests[1])

    def test_the_files_that_are_not_the_manifest_are_the_same(self):
        for name in ("Dockerfile", "pack_config.json", ".dockerignore"):
            a, b = (_read(os.path.join(ROOT, arch, ".service", name)) for arch in ARCHES)
            self.assertEqual(a, b, name)

    def test_pack_config_does_not_hide_the_docs_that_the_image_copies(self):
        # nodo applies the `ignore` of .service/pack_config.json to the whole tree: a `*.md`
        # there removes ide/SERVICE_CONFIG_GUIDE.md, which the Dockerfile copies. The patterns
        # that keep the context clean are in .dockerignore.
        for arch in ARCHES:
            config = json.loads(_read(os.path.join(ROOT, arch, ".service", "pack_config.json")))
            self.assertNotIn("*.md", config.get("ignore", []))

    def test_the_shared_sources_are_symlinks_to_the_root(self):
        for arch in ARCHES:
            for name in SHARED:
                link = os.path.join(ROOT, arch, name)
                self.assertTrue(os.path.islink(link), f"{arch}/{name}")
                self.assertEqual(os.path.realpath(link), os.path.join(ROOT, name))


if __name__ == "__main__":
    unittest.main()
