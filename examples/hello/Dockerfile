# Reproducible static-musl Rust reference service for the packer dogfood test.
#
# Determinism is the whole point of this example: `nodo pack examples/hello`
# must yield the SAME service-id every time. nodo's packer hashes filesystem
# metadata INCLUDING mtime_ns (see src/utils/filesystem_xattrs.py), so any
# build-wall-clock mtime left in the rootfs would change the id between two
# otherwise-identical builds. We therefore (1) build a dependency-free static
# musl binary from pinned-by-digest images, and (2) zero every mtime in the
# final rootfs.
#
# Both base images are pinned by digest so the rootfs BYTES (not just the tag)
# are identical across builds.

# ---- builder: static musl binary, no external crates -------------------------
FROM rust:1.83-alpine@sha256:0ac946ed7597a9f053a1be2ce38c09aa88b3d7079a91ea491493615294b1f699 AS builder
# crt-static is the musl default; remap the build path so it can't leak into the
# binary, and pin SOURCE_DATE_EPOCH for any timestamp-sensitive codegen.
ENV SOURCE_DATE_EPOCH=0 \
    CARGO_INCREMENTAL=0 \
    RUSTFLAGS="--remap-path-prefix=/build=."
# NOTE: context COPY sources MUST start with "./". nodo's packer rewrites every
# `COPY ./X ...` to `COPY service/X ...` (it nests the project under service/ in
# the build context); a source without the leading "./" is left untouched and
# then "not found". `COPY --from=<stage>` lines are correctly left alone.
RUN apk add --no-cache musl-dev
WORKDIR /build
COPY ./Cargo.toml ./
COPY ./src ./src
RUN cargo build --release --offline 2>/dev/null || cargo build --release \
    && cp target/release/hello /hello \
    && strip /hello

# ---- runtime: tiny rootfs with all mtimes normalised to epoch 0 --------------
FROM alpine:3.21@sha256:48b0309ca019d89d40f670aa1bc06e426dc0931948452e8491e3d65087abc07d
COPY --from=builder /hello /app/hello
# Normalise EVERY mtime to epoch 0. `-depth` processes a directory's children
# before the directory itself, so touching a child can't bump its parent's
# mtime back to build-time — the parent is re-zeroed afterwards. This makes the
# packed rootfs hash (and thus the service-id) reproducible.
RUN find / -xdev -depth -exec touch -h -d @0 {} + 2>/dev/null || true
