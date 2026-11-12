# linux/arm64 variant. nodo builds it with --opt platform=linux/arm64 (from
# service.json). amd64/.service/Dockerfile is the same file; only this comment
# is different.
#
# The build only makes a filesystem snapshot. CMD, ENTRYPOINT and EXPOSE are
# ignored. The entry point is set in service.json (init.entry_path), the port in
# service.json (api).
#
# Reproducibility is the point of this example: packing it twice must give the
# SAME service id. nodo hashes the metadata of the filesystem, mtime included
# (src/utils/filesystem_xattrs.py), so a build time left in the rootfs would
# change the id between two identical builds. So this builds a dependency-free
# static musl binary from images pinned by digest, and sets every mtime of the
# final rootfs to 0.
#
# Each FROM is pinned by the digest of its multi-arch index, so the same pin
# resolves to the image of the target architecture, and a tag that moves cannot
# change the image (and the service id) between two packs.

# ---- builder: static musl binary, no external crates -------------------------
FROM rust:1.83-alpine@sha256:0ac946ed7597a9f053a1be2ce38c09aa88b3d7079a91ea491493615294b1f699 AS builder
# crt-static is the musl default. Remap the build path so it cannot leak into the
# binary, and pin SOURCE_DATE_EPOCH for any code generation that reads the time.
ENV SOURCE_DATE_EPOCH=0 \
    CARGO_INCREMENTAL=0 \
    RUSTFLAGS="--remap-path-prefix=/build=."
RUN apk add --no-cache musl-dev
WORKDIR /build
# A COPY source of the project starts with "./": nodo rewrites it to
# `service/...` (it puts the project files in service/ of the build context). A
# source without the leading "./" is not rewritten, and is not found.
COPY ./Cargo.toml ./
COPY ./src ./src
RUN cargo build --release \
    && cp target/release/hello /hello \
    && strip /hello

# ---- runtime: a tiny rootfs, every mtime at epoch 0 --------------------------
FROM alpine:3.21@sha256:48b0309ca019d89d40f670aa1bc06e426dc0931948452e8491e3d65087abc07d
COPY --from=builder /hello /app/hello
# `-depth` handles the children of a directory before the directory, so touching
# a child cannot move the mtime of its parent back to the build time: the parent
# is set to 0 after it.
RUN find / -xdev -depth -exec touch -h -d @0 {} + 2>/dev/null || true
