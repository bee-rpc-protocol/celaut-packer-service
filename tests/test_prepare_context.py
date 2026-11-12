#!/usr/bin/env python3
"""The preparation that server.py does before the worker builds (what `nodo pack` does
on its side: prepare_directory + generate_service_zip) and the pre-flight check of
service.json against nodo's docs/PACKING.md. Nothing is built: no Docker, no BuildKit.

Run:  python3 -m unittest tests.test_prepare_context   (from the repo root)
"""
import json
import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="packer-ctx-test-")
os.environ["CACHE"] = os.path.join(_TMP, "cache")
os.makedirs(os.environ["CACHE"], exist_ok=True)

import server  # noqa: E402


def _write(root, rel, text=""):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def _read(root, rel):
    with open(os.path.join(root, rel)) as fh:
        return fh.read()


class StageContextTests(unittest.TestCase):
    def setUp(self):
        self.project = tempfile.mkdtemp(dir=_TMP)
        self.dest = os.path.join(tempfile.mkdtemp(dir=_TMP), "context")

    def test_service_folder_layout_gets_the_context_of_nodo(self):
        _write(self.project, ".service/Dockerfile",
               "FROM scratch\nCOPY ./src /app/src\nCOPY --from=b /x /y\nCOPY a.txt ./b.txt /z/\n")
        _write(self.project, ".service/service.json", "{}")
        _write(self.project, ".service/pack_config.json",
               json.dumps({"include": ["src", "keep.txt"], "ignore": ["*.pyc", "__pycache__/"]}))
        _write(self.project, ".service/.dockerignore", "secret/\n# comment\n")
        _write(self.project, "src/app.py", "print(1)")
        _write(self.project, "src/app.pyc", "x")
        _write(self.project, "src/__pycache__/a.pyc", "x")
        _write(self.project, "src/secret/key", "k")
        _write(self.project, "keep.txt", "k")
        _write(self.project, "other.txt", "not included")

        context = server._stage_context(self.project, self.dest)

        self.assertEqual(context, self.dest + "/")
        for name in ("Dockerfile", "service.json", "pack_config.json"):
            self.assertTrue(os.path.isfile(os.path.join(self.dest, name)), name)
        self.assertEqual(_read(self.dest, "service/src/app.py"), "print(1)")
        self.assertTrue(os.path.exists(os.path.join(self.dest, "service/keep.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.dest, "service/other.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.dest, "service/src/app.pyc")))
        self.assertFalse(os.path.exists(os.path.join(self.dest, "service/src/__pycache__")))
        self.assertFalse(os.path.exists(os.path.join(self.dest, "service/src/secret")))
        dockerfile = _read(self.dest, "Dockerfile")
        self.assertIn("COPY service/src /app/src", dockerfile)
        self.assertIn("COPY --from=b /x /y", dockerfile, "a stage is not the context")
        self.assertIn("COPY a.txt service/b.txt /z/", dockerfile, "only ./ sources move")

    def test_root_layout_is_prepared_too(self):
        _write(self.project, "Dockerfile", "FROM scratch\nCOPY ./src /s\n")
        _write(self.project, "service.json", "{}")
        _write(self.project, "src/main.go", "package main")
        server._stage_context(self.project, self.dest)
        self.assertIn("COPY service/src /s", _read(self.dest, "Dockerfile"))
        self.assertTrue(os.path.exists(os.path.join(self.dest, "service/src/main.go")))

    def test_what_nodo_sends_is_used_as_it_is(self):
        _write(self.project, "Dockerfile", "FROM scratch\nCOPY service/src /s\n")
        _write(self.project, "service.json", "{}")
        _write(self.project, "service/src/main.go", "package main")
        self.assertEqual(server._stage_context(self.project, self.dest), self.project)
        self.assertFalse(os.path.exists(self.dest))


class ValidateServiceConfigTests(unittest.TestCase):
    def _problems(self, service_json):
        project = tempfile.mkdtemp(dir=_TMP)
        _write(project, "service.json", json.dumps(service_json))
        return server._validate_service_config(project)

    def _ok(self, **extra):
        base = {"architecture": "linux/amd64", "init": {"entry_path": ["app", "start.sh"]}}
        base.update(extra)
        return base

    def test_a_current_service_json_has_no_problems(self):
        sj = self._ok(
            read_only_filesystem=True, uses_gateway=True, notice_declaration=False,
            api=[{"port": 8080, "protocol_stack": [["http2"], ["grpc"], {"tags": ["chat"], "prose": "p"}],
                  "mu_per_call": {"Solve": 100}}],
            network=[{"tags": ["*"], "prose": "egress"}],
            resources={"at_init": {"benchmark": {"int_ops_per_sec": 1}}},
            shared_filesystems=[{"path": "/shared", "role": "shared"}],
            initial_environment_workload=[{"any_of": []}],
        )
        self.assertEqual(self._problems(sj), [])

    def test_a_slot_without_protocol_is_allowed(self):
        self.assertEqual(self._problems(self._ok(api=[{"port": 80}])), [])

    def test_the_wrong_shapes_are_named(self):
        problems = " | ".join(self._problems(self._ok(
            read_only_filesystem="true",
            uses_gateway=1,
            notice_declaration="no",
            api=[{"port": 80, "protocol": ["http"], "protocol_stack": [["http"]]}],
            network=[{"tags": ["x"]}],
            resources={"at_most": {"benchmark": {}}},
            initial_environment_workload={},
        )))
        for text in ("read_only_filesystem", "uses_gateway", "notice_declaration",
                     "both", 'no "prose"', "resources.at_init.benchmark",
                     "initial_environment_workload"):
            self.assertIn(text.replace('no "prose"', 'has no "prose"'), problems)

    def test_an_entry_path_with_arguments_is_refused(self):
        problems = self._problems(self._ok(init={"entry_path": "app/start.sh --port 80"}))
        self.assertTrue(any("single path" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
