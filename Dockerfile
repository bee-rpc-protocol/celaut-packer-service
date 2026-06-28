# Celaut builds this image only to export its filesystem (docker buildx
# --output type=tar); the container is never run by Docker directly. CMD/
# ENTRYPOINT/EXPOSE are intentionally omitted — the entrypoint, ports and envs
# are declared in service.json. The packed service runs as a cloud-hypervisor
# microVM whose init runs /app/start.sh.
#
# Base = docker-in-docker: this is what lets the packer service do "the same as
# nodo pack" from *inside* a sealed microVM — it boots its own dockerd and runs
# `docker buildx build` to compile the image being packed. That nested build is
# why the service declares network: tag=(*) (buildx must pull base images).
FROM docker:27-dind

# --- Python + the real nodo packer (vendored, not re-implemented) -------------
# We clone the nodo source so we can invoke its own
# src/packers/zip_with_dockerfile.py worker; this guarantees the service-id we
# emit is byte-identical to `nodo pack`.
ARG NODO_REF=stable
ENV NODO_DIR=/opt/nodo \
    PYTHONUNBUFFERED=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1

# NOTE: docker:27-dind's baked python3.12 (3.12.13-r0) was built against a
# NEWER expat than the Alpine v3.21 repo currently ships: its pyexpat.so
# references XML_SetAllocTrackerActivationThreshold, a symbol the repo's
# libexpat lacks, so `import pyexpat` fails and pip (xmlrpc -> expat) is
# unusable. `apk add python3` is a no-op (already installed) so it keeps the
# mismatched baked build. Force a full `apk upgrade --available` so python3 AND
# expat are both reconciled to the same consistent v3.21 snapshot, THEN add the
# rest and sanity-check pyexpat — all before any pip invocation.
RUN apk update \
    && apk upgrade --no-cache --available \
    && apk add --no-cache \
        python3 py3-pip git bash tar \
        build-base python3-dev libffi-dev openssl-dev \
    && python3 -c "import pyexpat; print('pyexpat ok', pyexpat.EXPAT_VERSION)"

# Vendor the nodo tree (packer + its src.utils / protos / bee_rpc deps).
RUN git clone --depth 1 --branch "${NODO_REF}" \
        https://github.com/celaut-project/nodo.git "${NODO_DIR}"

# Determinism patches (REQUIRED so this service's ids match `nodo pack` on a
# patched node, and so packing the same project twice yields the same id).
# Upstream nodo's packer is nondeterministic for two reasons:
#   1. recursive_parsing iterates os.listdir() UNSORTED, so filesystem branch
#      order (hence the serialized bytes) varies between two extractions of the
#      same image tar.
#   2. it hashes mtime_ns, but tar extraction reassigns SYMLINK mtimes to the
#      current wall-clock time, so the id changes every pack.
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
#
# Two pins matter for a byte-identical service-id:
#   * protobuf==4.23.3 — the metadata serializer; matches the node's runtime and
#     the protoc that generated nodo's vendored protos/*_pb2.py.
#   * bee_rpc @ the exact git commit the node uses — owns the multiblock/.bee
#     block format that the id is hashed over.
# bee_rpc HARD-pins grpcio==1.56.0 + protobuf==4.23.3 in its metadata, but
# grpcio 1.56.0 has no cp312/musllinux wheel and would force a slow, fragile
# source build. grpcio is only the block-streaming transport underneath bee_rpc
# — it contributes no bytes to the hashed content — so we install bee_rpc with
# --no-deps and supply a wheel-shipping grpcio ourselves, while keeping protobuf
# pinned exactly.
RUN pip3 install --no-cache-dir \
        "protobuf==4.23.3" "grpcio" \
        "docker==6.1.3" requests requests-unixsocket "PyYAML==6.0.1" \
        "python-dotenv==1.0.0" psutil netifaces2 tabulate packaging \
        typing_extensions six \
    && pip3 install --no-cache-dir --no-deps \
        "git+https://github.com/bee-rpc-protocol/bee-rpc-over-grpc-py@7a2a344bc29546328bcf2f753dc2407f2c226376" \
    && python3 -c "from bee_rpc import client; import google.protobuf; print('deps ok, protobuf', google.protobuf.__version__)"

# --- Application --------------------------------------------------------------
WORKDIR /app
COPY ./server.py /app/server.py
COPY ./start.sh  /app/start.sh
RUN chmod +x /app/start.sh
