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

# Install the Python deps the packer imports. The nodo repo pins these in its
# requirements; we install that set plus bee_rpc explicitly. (First real build
# on an amd64 node will confirm the exact wheel set; adjust here if a transitive
# dep is missing — this is the one spot that may need a pin tweak.)
RUN pip3 install --no-cache-dir bee_rpc protobuf grpcio \
    && if [ -f "${NODO_DIR}/requirements.txt" ]; then \
         pip3 install --no-cache-dir -r "${NODO_DIR}/requirements.txt" || true ; \
       fi

# --- Application --------------------------------------------------------------
WORKDIR /app
COPY ./server.py /app/server.py
COPY ./start.sh  /app/start.sh
RUN chmod +x /app/start.sh
