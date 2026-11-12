#!/usr/bin/env python3
"""Unit tests for the "reduce resources usage" change (issue #6).

Two things are verified, both without Docker/KVM so they run anywhere:

  1. `service.json` reserves only 0.5 GB of RAM at BOTH ranges (at_init and
     at_most) — the packer no longer pins 4 GB up front.

  2. The RAM locker grows on demand through the node. `service.json` is only a
     0.5 GB floor now, so when a pack needs more than the currently-locked
     amount, `IOBigData.lock_ram` asks its nodo — the authority — to hotplug
     more memory (via `NodeControllerResourceManager.modify_resources`, which
     delegates to `node_controller.Controller.modify_resources` — the same call
     celaut-basics/demo-service uses) instead of failing against the older static
     system-available accounting.

The runtime-growth tests import `src.manager.resources`, which needs `psutil`
and the generated protos. On a bare box without them they SKIP rather than
error, mirroring the lazy-import contract of the sibling test modules.

Run:  python -m unittest tests.test_resource_growth   (from the repo root)
"""
import json
import subprocess
import tempfile
from unittest import mock
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_ROOT = os.path.join(_REPO_ROOT, "src")
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

HALF_GIB = 512 * 1024 * 1024  # 0.5 GB, the new reservation floor at both ranges

# Try to import the locker. Missing psutil/protobuf -> skip the runtime tests
# (the service.json test below has no such dependency and always runs).
try:
    import src.manager.resources as resources  # noqa: E402
    _HAVE_RESOURCES = True
    _IMPORT_ERR = None
except Exception as e:  # pragma: no cover - environment dependent
    resources = None
    _HAVE_RESOURCES = False
    _IMPORT_ERR = e


class ServiceJsonReservationTests(unittest.TestCase):
    """No deps: assert the declared reservation was cut to 0.5 GB at both ranges."""

    def setUp(self):
        with open(os.path.join(_REPO_ROOT, "amd64", ".service", "service.json")) as f:
            self.res = json.load(f)["resources"]

    def test_ram_is_half_gig_at_both_ranges(self):
        self.assertEqual(self.res["at_init"]["mem_limit"], HALF_GIB)
        self.assertEqual(self.res["at_most"]["mem_limit"], HALF_GIB)

    def test_at_init_equals_at_most(self):
        # Josemi's requirement: don't keep a high at_most; both ranges match.
        self.assertEqual(
            self.res["at_init"]["mem_limit"], self.res["at_most"]["mem_limit"]
        )
        self.assertEqual(
            self.res["at_init"]["disk_space"], self.res["at_most"]["disk_space"]
        )

    def test_disk_was_cut_hard(self):
        # Old floor was 20 GiB at both ranges. The current standard is 10 GiB
        # (the packer builds/seals heavier images inside the sealed VM and
        # ENOSPCs below this — see the init standard in
        # src/packers/zip_with_dockerfile.py). Still well under the old floor.
        TEN_GIB = 10 * 1024 * 1024 * 1024
        self.assertLessEqual(self.res["at_init"]["disk_space"], TEN_GIB)
        self.assertLessEqual(self.res["at_most"]["disk_space"], TEN_GIB)


class _FakeManager:
    """Stand-in for NodeControllerResourceManager: records requests and (optionally)
    'hotplugs' a shared pool holder so the static accounting sees more RAM."""

    def __init__(self, pool_holder=None, grant=True):
        self.calls = []
        self._pool = pool_holder
        self._grant = grant

    def modify_resources(self, min_bytes, max_bytes=None):
        self.calls.append((min_bytes, max_bytes))
        if not self._grant:
            return None  # node declined / unreachable -> static behaviour
        if self._pool is not None:
            self._pool["v"] = max(self._pool["v"], int(min_bytes))
        return int(min_bytes)


@unittest.skipUnless(_HAVE_RESOURCES, f"resources deps unavailable: {_IMPORT_ERR}")
class LockGrowOnDemandTests(unittest.TestCase):
    def setUp(self):
        # IOBigData is a Singleton; drop any cached instance so each test gets a
        # fresh locker with its own pool method / manager.
        resources.Singleton._instances.pop(resources.IOBigData, None)

    def _iobd(self, pool_holder, manager):
        return resources.IOBigData(
            log=lambda m: None,
            ram_pool_method=lambda: pool_holder["v"],
            resource_manager=manager,
        )

    def test_grows_via_node_when_static_pool_short(self):
        pool = {"v": 100 * 1024 * 1024}          # only 100 MB available statically
        mgr = _FakeManager(pool_holder=pool, grant=True)
        iobd = self._iobd(pool, mgr)

        need = 300 * 1024 * 1024                  # pack wants 300 MB
        iobd.lock_ram(ram_amount=need, wait=False)

        self.assertEqual(len(mgr.calls), 1, "node should be asked to grow exactly once")
        self.assertGreaterEqual(mgr.calls[0][0], need, "requested at least what the pack needs")
        self.assertEqual(iobd.ram_locked, need, "lock succeeds after the node grants RAM")
        self.assertGreaterEqual(pool["v"], need, "hotplugged pool now covers the lock")

    def test_static_failure_preserved_when_node_declines(self):
        pool = {"v": 100 * 1024 * 1024}
        mgr = _FakeManager(pool_holder=pool, grant=False)  # node can't/​won't grow
        iobd = self._iobd(pool, mgr)

        with self.assertRaises(Exception):
            iobd.lock_ram(ram_amount=300 * 1024 * 1024, wait=False)
        self.assertEqual(iobd.ram_locked, 0, "nothing locked when growth is unavailable")
        self.assertEqual(len(mgr.calls), 1, "growth was attempted before failing")

    def test_no_grow_when_static_pool_already_sufficient(self):
        pool = {"v": 1024 * 1024 * 1024}         # 1 GB available
        mgr = _FakeManager(pool_holder=pool, grant=True)
        iobd = self._iobd(pool, mgr)

        iobd.lock_ram(ram_amount=300 * 1024 * 1024, wait=False)

        self.assertEqual(mgr.calls, [], "node must not be bothered when RAM is already there")
        self.assertEqual(iobd.ram_locked, 300 * 1024 * 1024)

    def test_startup_locks_nothing(self):
        pool = {"v": HALF_GIB}
        iobd = self._iobd(pool, _FakeManager(pool_holder=pool))
        # Fresh locker holds no RAM: only the 0.5 GB reservation exists, and it
        # isn't consumed until a pack actually locks.
        self.assertEqual(iobd.ram_locked, 0)


@unittest.skipUnless(_HAVE_RESOURCES, f"resources deps unavailable: {_IMPORT_ERR}")
class NodeControllerResourceManagerTests(unittest.TestCase):
    def test_modify_resources_noops_without_config_file(self):
        # No /__config__ -> no node to ask -> returns None (static behaviour
        # kept). No child process is started.
        mgr = resources.NodeControllerResourceManager(
            config_file="/nonexistent/__config__", log=lambda m: None
        )
        with mock.patch.object(resources.subprocess, "run") as run:
            self.assertIsNone(mgr.modify_resources(HALF_GIB, HALF_GIB))
        run.assert_not_called()

    def _manager(self):
        config = tempfile.NamedTemporaryFile(suffix="-config", delete=False)
        config.close()
        self.addCleanup(os.unlink, config.name)
        return resources.NodeControllerResourceManager(
            config_file=config.name, app_dir="service", log=lambda m: None
        ), config.name

    def test_delegates_to_node_controller_in_a_child_process(self):
        # node_controller registers its own celaut.proto, which cannot share a
        # process with nodo's protos/. So the call runs in a child, like demo-service
        # makes it: Controller.modify_resources({'max': needed, 'min': 0}).
        mgr, config = self._manager()
        need = 600 * 1024 * 1024
        done = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"mem_limit": 700 * 1024 * 1024}) + "\n", stderr=""
        )
        with mock.patch.dict(os.environ, {"PYTHONPATH": "/opt/nodo"}), \
                mock.patch.object(resources.subprocess, "run", return_value=done) as run:
            granted = mgr.modify_resources(need, need)

        self.assertEqual(granted, 700 * 1024 * 1024,
                         "the granted mem_limit is read back from the child")
        (cmd,), kwargs = run.call_args
        self.assertEqual(cmd[:2], [sys.executable, "-c"])
        self.assertEqual(cmd[3:], [config, "service", str(need)])
        self.assertIn('{"max": target, "min": 0}', cmd[2],
                      "must send max=needed, min=0 exactly like demo-service")
        self.assertNotIn("PYTHONPATH", kwargs["env"], "the child must not import nodo's protos/")
        self.assertEqual(kwargs["cwd"], "/")

    def test_a_failed_child_answers_none(self):
        mgr, _ = self._manager()
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom\n")
        with mock.patch.object(resources.subprocess, "run", return_value=failed):
            self.assertIsNone(mgr.modify_resources(HALF_GIB, HALF_GIB))


if __name__ == "__main__":
    unittest.main()
