# Celaut builds this image only to export its filesystem (docker buildx
# --output type=tar); the container is never run by Docker directly. CMD/
# ENTRYPOINT/EXPOSE are intentionally omitted — the entrypoint, ports and envs
# are declared in service.json. The packed service runs as a cloud-hypervisor
# microVM whose init runs /app/start.sh.
#
# This service does "the same as `nodo pack`" from INSIDE a sealed microVM: it
# boots its own dockerd (docker-in-docker) and runs `docker buildx build` to
# compile the image being packed. That nested build is why service.json declares
# network: tag=(*) (buildx must pull base images).
#
# WHY A GLIBC (debian) BASE AND NOT docker:27-dind (alpine):
#   The packer vendors nodo, whose protobuf stack (bee_rpc's generated *_pb2)
#   requires protobuf's C backend (`google._upb`). protobuf 4.23.3 ships no
#   musllinux wheel with that extension, so on Alpine `import bee_rpc` dies with
#   "No module named 'google._upb'". On glibc the manylinux wheel provides it.
#   We still FORCE pure-python protobuf at runtime (see ENV below) for a stable,
#   deterministic xattrs-map serialization — but the package must be importable,
#   which needs glibc. The Docker engine itself is static, so we lift its
#   binaries out of the official docker:27-dind image (pinned by digest).

# ---- stage: official Docker-in-Docker (source of static engine binaries) -----
FROM docker:27-dind@sha256:aa3df78ecf320f5fafdce71c659f1629e96e9de0968305fe1de670e0ca9176ce AS dind

# ---- runtime: debian (glibc) -------------------------------------------------
FROM debian:bookworm-slim

ARG NODO_REF=stable
ENV NODO_DIR=/opt/nodo \
    PYTHONUNBUFFERED=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    # Force pure-python protobuf. protobuf MAP fields (the filesystem xattrs)
    # serialize in a DIFFERENT (and, for upb, time-unstable) byte order under the
    # C backend vs pure-python, which changes the service-id. The node packer is
    # pinned to pure-python too (pack_zip injects this into its worker), so both
    # sides serialize identically and reproducibly.
    PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python

# DinD runtime support tools (glibc side): iptables/iproute2 for networking,
# kmod for modprobe, e2fsprogs/xfsprogs for storage, pigz/xz for layer (de)compress.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-dev build-essential git ca-certificates \
        unzip zip \
        iptables iproute2 kmod e2fsprogs xfsprogs pigz xz-utils openssl uidmap \
    && rm -rf /var/lib/apt/lists/*

# Vendor the nodo tree (packer + its src.utils / protos / bee_rpc deps).
RUN git clone --depth 1 --branch "${NODO_REF}" \
        https://github.com/celaut-project/nodo.git "${NODO_DIR}"

# nodo resolves its docker tooling under NODO_ROOT (=/opt/nodo): bin/docker,
# bin/dockerd, libexec/docker/cli-plugins/docker-buildx, and the socket at
# docker/docker.sock — and RAISES if they're missing. Place the static engine
# binaries exactly there (after the clone, so the target dir merges cleanly).
COPY --from=dind /usr/local/bin/ /opt/nodo/bin/
COPY --from=dind /usr/local/libexec/ /opt/nodo/libexec/

# Determinism patches (REQUIRED so this service's ids match `nodo pack` on a
# patched node, and so packing the same project twice yields the same id).
# Upstream nodo's packer is nondeterministic two ways:
#   1. recursive_parsing iterates os.listdir() UNSORTED -> filesystem branch
#      order (hence serialized bytes) varies between extractions of the same tar.
#   2. it hashes mtime_ns, but tar extraction reassigns SYMLINK mtimes to the
#      current wall-clock time -> the id changes every pack.
# Both are content-irrelevant; normalise them. (Report upstream.)
RUN sed -i \
        's/for b_name in os.listdir(host_dir + directory):/for b_name in sorted(os.listdir(host_dir + directory)):/' \
        "${NODO_DIR}/src/packers/zip_with_dockerfile.py" \
    && sed -i \
        's/mtime_ns=int(stat_result.st_mtime_ns),/mtime_ns=0,/' \
        "${NODO_DIR}/src/utils/filesystem_xattrs.py" \
    && grep -q 'sorted(os.listdir(host_dir + directory))' \
        "${NODO_DIR}/src/packers/zip_with_dockerfile.py" \
    && grep -q 'mtime_ns=0,' "${NODO_DIR}/src/utils/filesystem_xattrs.py" \
    && echo "determinism patches applied"

# Install exactly the deps the packer worker (src/packers/zip_with_dockerfile)
# imports — NOT nodo's full requirements (those drag in the Ergo payment stack:
# ergpy/jpype1/coincurve/bip32/mnemonic, which need a JVM + libsecp256k1 and are
# never imported by the packer).
#   * protobuf==4.23.3 — matches the node's runtime + the protoc that generated
#     nodo's vendored protos/*_pb2.py.
#   * bee_rpc @ the exact git commit the node uses — owns the multiblock/.bee
#     block format the id is hashed over. Installed --no-deps so we control
#     protobuf/grpcio (bee_rpc hard-pins grpcio==1.56.0, which has no wheel here;
#     grpcio is only the streaming transport and contributes no hashed bytes).
RUN pip3 install --no-cache-dir \
        "protobuf==4.23.3" "grpcio" \
        "docker==6.1.3" requests requests-unixsocket "PyYAML==6.0.1" \
        "python-dotenv==1.0.0" psutil netifaces2 tabulate packaging \
        typing_extensions six mnemonic \
    && pip3 install --no-cache-dir --no-deps \
        "git+https://github.com/bee-rpc-protocol/bee-rpc-over-grpc-py@7a2a344bc29546328bcf2f753dc2407f2c226376" \
    && python3 -c "from bee_rpc import client; import google.protobuf; from google.protobuf.internal import api_implementation as a; assert a.Type()=='python', 'expected pure-python protobuf, got '+a.Type(); print('deps ok, protobuf', google.protobuf.__version__, a.Type())"

# Packer config. The vendored ConfigManager loads "config.yaml" relative to the
# worker's cwd (=NODO_DIR), and RAISES if absent; it also drives the SERVICE-ID
# (hash spec = sha3_256, MIN_BUFFER_BLOCK_SIZE = 10MB, packer arch support). This
# is the node's own config, sanitized (no ledgers/secrets/token) and re-pathed to
# /opt/nodo, so the service packs byte-identically to `nodo pack`.
COPY ./packer-config.yaml /opt/nodo/config.yaml

# --- Application --------------------------------------------------------------
WORKDIR /app
COPY ./server.py /app/server.py
COPY ./start.sh  /app/start.sh
RUN chmod +x /app/start.sh
