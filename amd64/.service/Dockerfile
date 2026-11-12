# linux/amd64 variant. nodo builds it with --opt platform=linux/amd64 (from
# service.json). arm64/.service/Dockerfile is the same file; only this comment
# is different.
#
# The build only makes a filesystem snapshot. CMD, ENTRYPOINT and EXPOSE are
# ignored. The entry point is set in service.json (init.entry_path), the ports in
# service.json (api). The packed service runs as a cloud-hypervisor microVM, and
# its init runs /app/start.sh.
#
# This service does "the same as `nodo pack`" from INSIDE a sealed microVM. It
# runs a BuildKit daemon in the guest and drives it with `buildctl`, as nodo's
# own packer does (src/utils/buildkit_env.py), so the service id of a pack here is
# the one of `nodo pack`. That build needs the network: service.json declares
# `network: tag=(*)` because BuildKit must pull the base images of the project.
#
# Every base image is pinned by digest. One digest of the multi-arch index serves
# both architectures.

# ---- stage: BuildKit (the daemon, its client and its runc) -------------------
# The same release that nodo installs (bash/install_buildkit.sh,
# NODO_BUILDKIT_VERSION). These binaries are static.
FROM moby/buildkit:v0.32.2@sha256:28a898719c18a33f4e8000685287fa36fd0dd9560c6440227d3a732d79bb41d8 AS buildkit

# ---- stage: qemu user emulation ----------------------------------------------
# `binfmt --install` registers the qemu handlers in the kernel of the guest. With
# them, a RUN line of a linux/arm64 build runs on an amd64 guest (and the other way
# round), so this packer builds both architectures. src/utils/arch_guard.py checks
# the handler before a foreign build starts.
FROM tonistiigi/binfmt:qemu-v9.2.2-52@sha256:1b804311fe87047a4c96d38b4b3ef6f62fca8cd125265917a9e3dc3c996c39e6 AS binfmt

# ---- stage: only the qemu binaries that are needed ----------------------------
# The image has the qemu of nine architectures, 8 MB each, and only the ones of
# the architectures of the other one are in it. Keep `binfmt` and the two that this
# packer can build (the one of the native architecture is not in the image: it is
# not needed there, and it is not copied).
FROM debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171 AS qemu
COPY --from=binfmt /usr/bin/ /all/
RUN mkdir /q && cd /all && cp -a binfmt /q/ && for f in qemu-aarch64 qemu-x86_64; do [ -e "$f" ] && cp -a "$f" /q/ || true; done

# ---- stage: Python dependencies ----------------------------------------------
# pip and git are only needed to install the dependencies. This stage installs
# them, and the runtime stage gets the installed packages only (no pip, no git, no
# perl). Same base as the runtime stage, so the same Python.
FROM debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171 AS pydeps
ENV PIP_BREAK_SYSTEM_PACKAGES=1
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
#   * protobuf==4.23.3 — matches the protoc (23.1) that generated protos/*_pb2.py.
#   * bee_rpc at v0.0.3, the release that nodo's requirements pin — owns the
#     multiblock/.bee block format the id is hashed over. --no-deps: it hard-pins
#     grpcio==1.56.0, which has no wheel here; grpcio is only the streaming
#     transport and contributes no hashed bytes.
#   * cryptography, mnemonic, psutil, requests — imported by the modules of nodo
#     that the packer imports (identity, RAM accounting, routing).
#   * node_controller (celaut-project/libraries) — the canonical grow-on-demand
#     client. src/manager/resources.py builds a Controller from /__config__ and
#     calls Controller.modify_resources() to hotplug RAM (as celaut-basics/
#     demo-service does), in a child process: node_controller registers its own
#     copy of celaut.proto, which cannot share a process with nodo's protos/.
#     --no-deps: its only runtime dependency is bee_rpc.
# These are the dependencies that the packer worker imports, NOT nodo's full
# requirements (those drag in the Ergo payment stack: ergpy/jpype1/coincurve/bip32,
# which need a JVM and libsecp256k1 and are never imported by the packer).
RUN pip3 install --no-cache-dir \
        "protobuf==4.23.3" "grpcio" \
        requests psutil mnemonic cryptography \
    && pip3 install --no-cache-dir --no-deps \
        "git+https://github.com/bee-rpc-protocol/bee-rpc-over-grpc-py@v0.0.3" \
    && pip3 install --no-cache-dir --no-deps \
        "git+https://github.com/celaut-project/libraries" \
    && find /usr/local/lib/python3.11/dist-packages -name __pycache__ -type d -prune -exec rm -rf {} + \
    && find /usr/local/lib/python3.11/dist-packages -name tests -type d -prune -exec rm -rf {} +

# ---- runtime: debian (glibc) -------------------------------------------------
# WHY A GLIBC (debian) BASE: the packer imports bee_rpc, whose protobuf stack needs
# the C backend (`google._upb`) to be importable, and protobuf 4.23.3 has no
# musllinux wheel with it. We still FORCE pure-python protobuf at runtime (see ENV
# below) for a stable serialization of the xattrs map.
FROM debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171

ENV NODO_DIR=/opt/nodo \
    PYTHONUNBUFFERED=1 \
    # Force pure-python protobuf. protobuf MAP fields (the filesystem xattrs)
    # serialize in a DIFFERENT (and, for upb, time-unstable) byte order under the
    # C backend vs pure-python, which changes the service-id. The node packer is
    # pinned to pure-python too (pack_zip injects this into its worker), so both
    # sides serialize identically and reproducibly.
    PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python

# Runtime: Python and the CA certificates. Nothing else: BuildKit runs on the host
# network with its own runc, and the project zip is read with Python's zipfile. (The
# Docker engine that this service used to run needed iptables, iproute2, kmod,
# e2fsprogs, xfsprogs and openssl; BuildKit does not.) The docs, man pages and
# locales are removed: the image is read by nodo, not by a person.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt /var/log/apt /var/log/dpkg.log \
        /usr/share/doc /usr/share/man /usr/share/info /usr/share/locale /usr/share/lintian \
    && find /usr/lib/python3* -name __pycache__ -type d -prune -exec rm -rf {} +

# The installed dependencies of the stage above.
COPY --from=pydeps /usr/local/lib/python3.11/dist-packages /usr/local/lib/python3.11/dist-packages

# The packer source lives IN-REPO under src/ (self-contained: nodo is not cloned at
# build time). It carries the import closure of the packer worker
# `src.packers.zip_with_dockerfile` of nodo's `dev` branch, plus protos/. The outer
# src/ mirrors the /opt/nodo payload; the inner src/ is the Python package root.
# See src/README.md.
COPY ./src/ /opt/nodo/

# nodo looks for BuildKit under NODO_ROOT/bin (src/utils/buildkit_env.py): buildctl
# to build, buildkitd for the daemon, and buildkit-runc next to buildkitd (the
# daemon finds it through PATH). One COPY for each file: nodo's build-context path
# rewriter only prefixes the FIRST source of a multi-source COPY.
COPY --from=buildkit /usr/bin/buildctl /opt/nodo/bin/buildctl
COPY --from=buildkit /usr/bin/buildkitd /opt/nodo/bin/buildkitd
COPY --from=buildkit /usr/bin/buildkit-runc /opt/nodo/bin/buildkit-runc

# The `binfmt` installer and the qemu-user binaries of the two architectures that
# this packer builds (start.sh registers the one of the other architecture).
COPY --from=qemu /q/ /opt/binfmt/

# Three fixes are baked into the packer source (search for "DETERMINISM PATCH" and
# "BUILDER MOUNT POINT PATCH" in src/): the filesystem branches are sorted, so the
# serialized bytes do not depend on the order that the extraction gives; the mtime
# of a symlink is 0, because tar gives a symlink the time of the extraction; and
# /sys is exported at epoch 0, because this service runs BuildKit as root and that
# daemon names the build time there, while nodo's rootless one does not. Assert
# all three, so that a refresh from nodo that drops one fails the build, not the ids.
RUN grep -q 'sorted(os.listdir(host_dir + directory))' \
        "${NODO_DIR}/src/packers/zip_with_dockerfile.py" \
    && grep -q 'mtime_ns=0,' "${NODO_DIR}/src/utils/filesystem_xattrs.py" \
    && grep -q 'BUILDER_MOUNT_POINTS_AT_EPOCH' \
        "${NODO_DIR}/src/packers/zip_with_dockerfile.py" \
    && echo "packer source fixes present"

# Check that the packer worker and node_controller import (in two processes: their
# celaut.proto cannot share one) and that protobuf is the pure-python one.
RUN cd /opt/nodo \
    && CACHE=/tmp/imp-cache/ BLOCKDIR=/tmp/imp-blocks/ STORAGE=/tmp/imp-store \
        python3 -B -c "import src.packers.zip_with_dockerfile; from google.protobuf.internal import api_implementation as a; assert a.Type()=='python', 'expected pure-python protobuf, got '+a.Type(); print('deps ok: the packer worker imports, protobuf', a.Type())" \
    && cd / \
    && python3 -B -c "from node_controller.controller.controller import Controller; print('node_controller import ok')" \
    && rm -rf /tmp/imp-cache /tmp/imp-blocks /tmp/imp-store

# No config.yaml. A config file makes sense on a full nodo, not on a service that
# does one thing: src/utils/config.py answers the few keys that the packer reads
# from a table of defaults, and start.sh sets the directories. What decides the
# id is the same as in nodo: the hash is sha3_256 (hashing.HASH), and the size
# above which a file is a block does not change the id.

# --- Application --------------------------------------------------------------
WORKDIR /app
COPY ./server.py /app/server.py
COPY ./start.sh  /app/start.sh
RUN chmod +x /app/start.sh
