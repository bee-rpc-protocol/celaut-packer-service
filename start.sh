#!/bin/bash
# Celaut init entrypoint for the packer service (declared in service.json ->
# init.entry_path). The microVM init runs this as PID 1; the Docker CMD/ENTRYPOINT
# of the image that packs THIS service are ignored.
#
# Sequence:
#   1. Start a BuildKit daemon in the guest, on the socket that nodo's packer
#      expects (NODO_DIR/buildkit/buildkitd.sock, see src/utils/buildkit_env.py).
#      The worker drives it with `buildctl`, exactly as `nodo pack` does.
#   2. Register the qemu handlers of the other architecture, so a linux/arm64 pack
#      runs on an amd64 guest and the other way round.
#   3. Export the env that the packer reads (src/utils/config.py).
#   4. Start the HTTP packer front-end (:8080).
#   5. Read the notices of the node (/dev/hvc0, service.json -> notice_declaration):
#      on STOP or HIBERNATE, stop the server and BuildKit, and exit, so that the
#      node ends the wait at once.
set -e

export NODO_DIR="${NODO_DIR:-/opt/nodo}"
export MAIN_DIR="${NODO_DIR}"
export PYTHONPATH="${NODO_DIR}:${PYTHONPATH}"
# BuildKit binaries live under NODO_DIR/bin (see Dockerfile). buildkitd finds
# buildkit-runc through PATH.
export PATH="${NODO_DIR}/bin:${PATH}"

export CACHE="${CACHE:-/var/lib/celaut/cache/}"
export BLOCKDIR="${BLOCKDIR:-/var/lib/celaut/blocks/}"
export STORAGE="${STORAGE:-/var/lib/celaut}"
# Packer tunables (src/utils/config.py). A file at or over MIN_BUFFER_BLOCK_SIZE is
# a block on disk; a smaller one is kept in memory while the service is packed.
# It does not change the service id. Low: the guest has 0.5 GB of RAM at the start.
export MIN_BUFFER_BLOCK_SIZE="${MIN_BUFFER_BLOCK_SIZE:-32768}"
export PACKER_MEMORY_SIZE_FACTOR="${PACKER_MEMORY_SIZE_FACTOR:-6.0}"
export SAVE_ALL="${SAVE_ALL:-False}"
# Force pure-python protobuf so the xattrs map serializes in the same byte order
# as the node packer (also pinned to pure-python) -> matching, stable service-id.
export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION="python"

BUILDKIT_DIR="${NODO_DIR}/buildkit"
BUILDKIT_SOCKET="${BUILDKIT_DIR}/buildkitd.sock"
BUILDKIT_ROOT="${BUILDKIT_DIR}/data"
BUILDKIT_LOG=/var/log/buildkitd.log
export BUILDKIT_SOCKET

mkdir -p "$CACHE" "$BLOCKDIR" "$STORAGE" "$BUILDKIT_ROOT"

# The CH guest's kernel ip= boot path points resolv.conf at the nodo gateway,
# but there is no DNS proxy listening there. Use public resolvers over the
# wildcard egress channel so BuildKit itself can resolve registries.
printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' >/etc/resolv.conf || true

# --- BuildKit -----------------------------------------------------------------
BUILDKITD_PID=""

buildkit_ready() {
  buildctl --addr "unix://${BUILDKIT_SOCKET}" debug workers >/dev/null 2>&1
}

start_buildkitd() {
  local snapshotter="$1"
  echo "[start] starting buildkitd (snapshotter: ${snapshotter}) on ${BUILDKIT_SOCKET} ..."
  rm -f "${BUILDKIT_SOCKET}"
  # --oci-worker-net host: the build uses the network of the guest (the `*` egress
  # channel of service.json). The same as nodo's own builder.
  buildkitd \
      --root "${BUILDKIT_ROOT}" \
      --addr "unix://${BUILDKIT_SOCKET}" \
      --oci-worker-net host \
      --oci-worker-snapshotter "${snapshotter}" \
      >"${BUILDKIT_LOG}" 2>&1 &
  BUILDKITD_PID=$!
}

wait_for_buildkitd() {
  local tries=0
  until buildkit_ready; do
    kill -0 "${BUILDKITD_PID}" 2>/dev/null || return 1
    tries=$((tries + 1))
    [ "$tries" -gt 60 ] && return 1
    sleep 1
  done
}

stop_buildkitd() {
  if [ -n "${BUILDKITD_PID}" ] && kill -0 "${BUILDKITD_PID}" 2>/dev/null; then
    kill -TERM "${BUILDKITD_PID}" 2>/dev/null || true
    wait "${BUILDKITD_PID}" 2>/dev/null || true
  fi
}

# overlayfs when the guest kernel has it; the native (copy) snapshotter otherwise.
start_buildkitd overlayfs
if ! wait_for_buildkitd; then
  echo "[start] buildkitd with overlayfs did not start; retrying with the native snapshotter." >&2
  tail -n 40 "${BUILDKIT_LOG}" >&2 || true
  stop_buildkitd
  start_buildkitd native
  if ! wait_for_buildkitd; then
    echo "[start] buildkitd did not become ready in time; last log:" >&2
    tail -n 80 "${BUILDKIT_LOG}" >&2 || true
    exit 1
  fi
fi
echo "[start] buildkitd ready."

# --- qemu/binfmt for cross-arch emulation -------------------------------------
# To pack a service of the other architecture the guest must emulate it. Register
# the qemu handler in the kernel (the binaries were copied to /opt/binfmt by the
# Dockerfile). Idempotent; gated by PACKER_ENABLE_QEMU (default on).
if [ "${PACKER_ENABLE_QEMU:-1}" = "1" ]; then
  case "$(uname -m)" in
    x86_64|amd64)  foreign=arm64 ;;
    aarch64|arm64) foreign=amd64 ;;
    *)             foreign="" ;;
  esac
  if [ -n "$foreign" ]; then
    mountpoint -q /proc/sys/fs/binfmt_misc 2>/dev/null \
      || mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc 2>/dev/null || true
    if (cd /opt/binfmt && ./binfmt --install "$foreign") >/var/log/binfmt.log 2>&1; then
      echo "[start] qemu binfmt ${foreign} registered:"
      ls /proc/sys/fs/binfmt_misc/ 2>/dev/null | grep -i 'qemu-' \
        | sed 's/^/[start]   /' || echo "[start]   (handler not visible in binfmt_misc)"
    else
      echo "[start] WARN: qemu binfmt install failed — ${foreign} packing unavailable." >&2
      tail -n 20 /var/log/binfmt.log >&2 || true
    fi
  fi
fi

# --- The packer API -----------------------------------------------------------
echo "[start] launching packer HTTP server on :8080"
python3 /app/server.py &
SERVER_PID=$!

# --- Notices of the node ------------------------------------------------------
# service.json declares the default notice (/dev/hvc0). The node writes one line,
# `VERB <t>`, before it stops or hibernates the service, and waits until the unix
# time <t> (15 s by default). Stop the server and BuildKit and exit before that:
# PID 1 that exits tells the node that we are done.
# Open the device once and keep it open; a hangup of the tty never kills PID 1.
trap '' HUP
shutdown() {
  echo "[start] $1 notice received: stopping."
  kill -TERM "${SERVER_PID}" 2>/dev/null || true
  wait "${SERVER_PID}" 2>/dev/null || true
  stop_buildkitd
  sync
  exit 0
}

if [ -c /dev/hvc0 ]; then
  while IFS= read -r line; do
    set -- $line
    case "$1" in
      STOP|HIBERNATE) shutdown "$1" ;;
      *) ;;  # a verb that this service does not know: ignore it
    esac
  done < /dev/hvc0
fi
# No /dev/hvc0 (an older node), or it closed: serve until the server ends.
wait "${SERVER_PID}"
