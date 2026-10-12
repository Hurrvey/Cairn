# MinIO community binaries/images are not assumed available. Build a fixed
# official source revision; this optional service remains independently licensed.
FROM golang:1.25-bookworm AS builder
ENV CGO_ENABLED=0
RUN go install github.com/minio/minio@v0.0.0-20250422221226-0d7408fc9969

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /go/bin/minio /usr/local/bin/minio
EXPOSE 9000 9001
ENTRYPOINT ["minio"]
CMD ["server", "/data", "--console-address", ":9001"]
