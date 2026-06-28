#!/usr/bin/env python3
"""
Celaut Packer Service — HTTP front-end.

A network-facing Celaut microVM service that does *the same thing the nodo
packer does* (src/packers/zip_with_dockerfile.py): given a project archive
containing a Dockerfile, a packer config and a service.json, it builds the
image, exports its filesystem, serialises a content-addressed `celaut.Service`
protobuf, and returns the packed service as a single `.celaut.bee` file.

It does NOT re-implement the packer. It vendors the real nodo packer (cloned
into /opt/nodo at image-build time) and invokes its worker entrypoint, so the
service-id produced here is byte-identical to `nodo pack`. Docker/buildx runs
inside this microVM (docker-in-docker); that is why the service declares
`network: tag=(*)` — buildx must reach arbitrary registries to pull base images.

API (bound on 0.0.0.0:8080):
  GET  /health            -> 200 "ok"
  POST /pack              -> body = project .zip (application/zip | octet-stream)
                             200 -> application/octet-stream, the `.celaut.bee`
                                    headers: X-Service-Id: <hex>
                                             Content-Disposition: attachment;
                                                 filename="<service_id>.celaut.bee"
                             400 -> packing/validation error (text body)
                             415 -> bad/missing zip

The zip must contain (at its root, or in a single top-level folder):
  - Dockerfile
  - service.json
  - pack_config.json   (optional; honoured by the nodo packer's ignore globs)
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Where the nodo source tree is vendored in the image (see Dockerfile).
NODO_DIR = os.environ.get("NODO_DIR", "/opt/nodo")
PORT = int(os.environ.get("PORT", "8080"))
MAX_ZIP_BYTES = int(os.environ.get("MAX_ZIP_BYTES", str(512 * 1024 * 1024)))  # 512MB

# The nodo ConfigManager reads these; start.sh exports them too. Kept here so the
# server is runnable/inspectable on its own.
os.environ.setdefault("CACHE", "/var/lib/celaut/cache/")
os.environ.setdefault("BLOCKDIR", "/var/lib/celaut/blocks/")


def _flatten_single_root(extract_dir: str) -> str:
    """If the zip wrapped everything in one top-level folder, descend into it so
    the packer finds service.json/Dockerfile at `path`. Mirrors how `nodo pack`
    treats a project directory."""
    entries = [e for e in os.listdir(extract_dir) if not e.startswith("__MACOSX")]
    if len(entries) == 1:
        only = os.path.join(extract_dir, entries[0])
        if os.path.isdir(only) and os.path.exists(os.path.join(only, "service.json")):
            return only + "/"
    return extract_dir.rstrip("/") + "/"


def _run_packer(zip_path: str):
    """Invoke the real nodo packer worker out-of-process (exactly as nodo's
    pack_zip does) and return (service_id, metadata_bytes, service_dir) or raise.

    The worker writes a JSON result file: {service_id, metadata_b64, service_dir}
    or {error}.
    """
    result_path = os.path.join(
        os.environ["CACHE"], f"pack_result_{uuid.uuid4().hex}.json"
    )
    os.makedirs(os.path.dirname(result_path), exist_ok=True)
    cmd = [
        sys.executable, "-m", "src.packers.zip_with_dockerfile",
        "--worker", zip_path, result_path,
    ]
    proc = subprocess.run(cmd, cwd=NODO_DIR)
    if proc.returncode != 0:
        raise RuntimeError(f"packer worker exited {proc.returncode}")
    if not os.path.exists(result_path):
        raise RuntimeError("packer worker produced no result file")
    with open(result_path) as f:
        result = json.load(f)
    os.remove(result_path)
    if result.get("error"):
        raise RuntimeError(result["error"])
    metadata_b64 = result.get("metadata_b64")
    return (
        result["service_id"],
        base64.b64decode(metadata_b64) if metadata_b64 else b"",
        result["service_dir"],
    )


def _serialize_bee(service_dir: str) -> bytes:
    """Serialise the packer's multiblock directory into a single `.celaut.bee`
    byte stream in the framing `nodo import` expects: a sequence of
    buffer_pb2.Buffer messages, each preceded by its length as a 4-byte
    big-endian integer (see bee_rpc.client write_to_file / reader.read_bee_file).

    read_from_registry walks the multiblock dir and yields the Buffer objects
    (chunk= for inline bytes, block= for block pointers); we just length-prefix
    each. Concatenating raw multiblock chunks (the previous approach) is NOT a
    valid .bee and fails import with "Incomplete message data"."""
    # Imported lazily so the module is importable without the nodo deps present
    # (e.g. for unit-testing the HTTP layer on a dev box).
    sys.path.insert(0, NODO_DIR)
    from bee_rpc import client as grpcbb  # type: ignore
    buf = bytearray()
    for b in grpcbb.read_from_registry(filename=service_dir):
        data = b.SerializeToString()
        buf.extend(len(data).to_bytes(4, "big"))
        buf.extend(data)
    return bytes(buf)


class Handler(BaseHTTPRequestHandler):
    server_version = "celaut-packer/0.1"

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt, *args):  # quieter, single-line logs to stderr
        sys.stderr.write("[packer] " + (fmt % args) + "\n")

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", "/healthz", ""):
            return self._send(200, "ok")
        return self._send(404, "not found")

    def do_POST(self):
        if self.path.rstrip("/") != "/pack":
            return self._send(404, "not found")
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return self._send(415, "empty body; POST a project .zip")
        if length > MAX_ZIP_BYTES:
            return self._send(413, f"zip too large (> {MAX_ZIP_BYTES} bytes)")

        work = tempfile.mkdtemp(prefix="pack-", dir=os.environ["CACHE"])
        zip_path = os.path.join(work, "input.zip")
        try:
            remaining = length
            with open(zip_path, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)

            if not zipfile.is_zipfile(zip_path):
                return self._send(415, "body is not a valid zip archive")

            extract_dir = os.path.join(work, "src")
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(extract_dir)
            project = _flatten_single_root(extract_dir)
            if not os.path.exists(os.path.join(project, "service.json")):
                return self._send(400, "zip missing service.json at root")
            if not os.path.exists(os.path.join(project, "Dockerfile")):
                return self._send(400, "zip missing Dockerfile at root")

            # Re-zip the (possibly flattened) project for the worker, which unzips
            # to its own cache. Mirrors nodo's zipfile_ok contract.
            norm_zip = os.path.join(work, "project.zip")
            with zipfile.ZipFile(norm_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, _, files in os.walk(project):
                    for name in files:
                        ap = os.path.join(root, name)
                        zf.write(ap, os.path.relpath(ap, project))

            service_id, _meta, service_dir = _run_packer(norm_zip)
            bee = _serialize_bee(service_dir)

            fname = f"{service_id}.celaut.bee"
            return self._send(
                200, bee,
                ctype="application/octet-stream",
                headers={
                    "X-Service-Id": service_id,
                    "Content-Disposition": f'attachment; filename="{fname}"',
                },
            )
        except RuntimeError as e:
            return self._send(400, f"pack failed: {e}")
        except Exception as e:  # noqa: BLE001 - surface unexpected failures
            return self._send(500, f"internal error: {e}")
        finally:
            subprocess.run(["rm", "-rf", work], check=False)


def main():
    os.makedirs(os.environ["CACHE"], exist_ok=True)
    os.makedirs(os.environ["BLOCKDIR"], exist_ok=True)
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    sys.stderr.write(f"[packer] listening on 0.0.0.0:{PORT}\n")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
