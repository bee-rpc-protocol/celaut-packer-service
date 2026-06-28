#!/bin/bash
# Celaut init entrypoint for the packer service (declared in service.json ->
# init.entry_path). The microVM init runs this; Docker CMD/ENTRYPOINT are ignored
# by the packer that builds THIS image.
#
# Sequence:
#   1. Boot an in-VM Docker daemon (docker-in-docker) on the socket nodo expects
#      (NODO_ROOT/docker/docker.sock), with the vfs storage driver.
#   2. Create a host-network buildx builder under nodo's DOCKER_CONFIG so the
#      vendored packer worker (which shells out to `docker buildx build
#      --builder nodo-hostnet`) finds it.
#   3. Export the env the nodo ConfigManager / packer expect.
#   4. Launch the HTTP packer front-end on 0.0.0.0:8080.
set -e

export NODO_DIR="${NODO_DIR:-/opt/nodo}"
export MAIN_DIR="${NODO_DIR}"
export PYTHONPATH="${NODO_DIR}:${PYTHONPATH}"
# Static Docker engine binaries live under NODO_DIR/bin (see Dockerfile).
export PATH="${NODO_DIR}/bin:${PATH}"
# nodo's runtime.py points DOCKER_CONFIG / buildx state here; create the builder
# with the SAME config dir so the worker sees the nodo-hostnet builder.
export DOCKER_CONFIG="${NODO_DIR}/libexec/docker"
export DOCKER_HOST="unix://${NODO_DIR}/docker/docker.sock"

export CACHE="${CACHE:-/var/lib/celaut/cache/}"
export BLOCKDIR="${BLOCKDIR:-/var/lib/celaut/blocks/}"
# Packer tunables consumed by ConfigManager (safe defaults mirroring nodo).
export PACKER_MEMORY_SIZE_FACTOR="${PACKER_MEMORY_SIZE_FACTOR:-2}"
export MIN_BUFFER_BLOCK_SIZE="${MIN_BUFFER_BLOCK_SIZE:-1048576}"
export SAVE_ALL="${SAVE_ALL:-False}"
# Build with the host-network builder so buildx egress uses the VM's (*) channel.
export BUILDX_BUILDER="${BUILDX_BUILDER:-nodo-hostnet}"
# Force pure-python protobuf so the xattrs map serializes in the same byte order
# as the node packer (also pinned to pure-python) -> matching, stable service-id.
export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION="python"

mkdir -p "$CACHE" "$BLOCKDIR" \
    "${NODO_DIR}/docker/data" "${NODO_DIR}/docker/exec" "${DOCKER_CONFIG}"

# cgroup v2: dind needs a writable cgroup tree. In a privileged container/microVM
# /sys/fs/cgroup is normally already mounted as cgroup2; mount it if not.
if [ ! -e /sys/fs/cgroup/cgroup.controllers ] && ! mountpoint -q /sys/fs/cgroup; then
  mount -t cgroup2 none /sys/fs/cgroup 2>/dev/null || true
fi

# Remove any stale pidfile/socket from a previous boot (e.g. a microVM/container
# restart) so dockerd doesn't refuse to start with "process N is still running".
rm -f "${NODO_DIR}/docker/docker.pid" "${NODO_DIR}/docker/docker.sock"

echo "[start] booting in-VM dockerd (vfs) on ${NODO_DIR}/docker/docker.sock ..."
# --storage-driver=vfs: overlay2 frequently can't mount inside a cloud-hypervisor
# microVM's root fs; vfs always works (slower/heavier but correct).
# --iptables=false: the buildx builder runs with --network host (below), so the
# nested build egresses via the VM's own network and needs no bridge NAT.
"${NODO_DIR}/bin/dockerd" \
    --host="unix://${NODO_DIR}/docker/docker.sock" \
    --data-root="${NODO_DIR}/docker/data" \
    --exec-root="${NODO_DIR}/docker/exec" \
    --pidfile="${NODO_DIR}/docker/docker.pid" \
    --storage-driver=vfs \
    --iptables=false \
    >/var/log/dockerd.log 2>&1 &

# Wait for the daemon socket to come up before packing.
tries=0
until docker info >/dev/null 2>&1; do
  tries=$((tries + 1))
  if [ "$tries" -gt 180 ]; then
    echo "[start] dockerd did not become ready in time; last log:" >&2
    tail -n 60 /var/log/dockerd.log >&2 || true
    exit 1
  fi
  sleep 1
done
echo "[start] dockerd ready."

# Host-network buildx builder (matches nodo's packer.docker.BUILDX_BUILDER).
if ! docker buildx inspect "$BUILDX_BUILDER" >/dev/null 2>&1; then
  docker buildx create --name "$BUILDX_BUILDER" \
    --driver docker-container --driver-opt network=host >/dev/null 2>&1 || true
fi
docker buildx inspect "$BUILDX_BUILDER" --bootstrap >/dev/null 2>&1 || true
echo "[start] buildx builder '$BUILDX_BUILDER' ready."

echo "[start] launching packer HTTP server on :8080"
exec python3 /app/server.py
