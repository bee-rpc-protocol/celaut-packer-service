#!/bin/bash
# Celaut init entrypoint for the packer service (declared in service.json ->
# init.entry_path). The microVM init runs this; Docker CMD/ENTRYPOINT are ignored
# by the packer that builds THIS image.
#
# Sequence:
#   1. Boot a native Docker daemon inside the cloud-hypervisor guest, on the
#      socket nodo expects (NODO_ROOT/docker/docker.sock).
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

# Remove any stale pidfile/socket from a previous boot (e.g. a microVM/container
# restart) so dockerd doesn't refuse to start with "process N is still running".
rm -f "${NODO_DIR}/docker/docker.pid" "${NODO_DIR}/docker/docker.sock"

# The CH guest's kernel ip= boot path points resolv.conf at the nodo gateway,
# but there is no DNS proxy listening there. Use public resolvers over the
# wildcard egress channel so dockerd itself can resolve registries.
printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' >/etc/resolv.conf || true

start_dockerd() {
  local storage_driver="$1"
  echo "[start] booting native dockerd (${storage_driver}) on ${NODO_DIR}/docker/docker.sock ..."
  "${NODO_DIR}/bin/dockerd" \
      --host="unix://${NODO_DIR}/docker/docker.sock" \
      --data-root="${NODO_DIR}/docker/data" \
      --exec-root="${NODO_DIR}/docker/exec" \
      --pidfile="${NODO_DIR}/docker/docker.pid" \
      --bridge=none \
      --iptables=false \
      --ip6tables=false \
      --dns=1.1.1.1 \
      --dns=8.8.8.8 \
      --storage-driver="${storage_driver}" \
      >/var/log/dockerd.log 2>&1 &
}

wait_for_dockerd() {
  local tries=0
  until docker info >/dev/null 2>&1; do
    tries=$((tries + 1))
    if [ "$tries" -gt 90 ]; then
      return 1
    fi
    sleep 1
  done
}

start_dockerd overlay2

if ! wait_for_dockerd; then
  echo "[start] dockerd overlay2 startup failed; retrying with vfs fallback." >&2
  tail -n 40 /var/log/dockerd.log >&2 || true
  if [ -f "${NODO_DIR}/docker/docker.pid" ]; then
    kill "$(cat "${NODO_DIR}/docker/docker.pid")" 2>/dev/null || true
  fi
  sleep 2
  rm -f "${NODO_DIR}/docker/docker.pid" "${NODO_DIR}/docker/docker.sock"
  start_dockerd vfs
  if ! wait_for_dockerd; then
    echo "[start] dockerd did not become ready in time; last log:" >&2
    tail -n 80 /var/log/dockerd.log >&2 || true
    exit 1
  fi
fi
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
