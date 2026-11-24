# Builds the cfsolve-backed page fetcher, then the Python service that shells out to it.
FROM rust:1.98-bookworm AS fetch-builder

RUN apt-get update \
    && apt-get install --yes --no-install-recommends cmake libclang-dev clang git perl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY Cargo.toml Cargo.lock rust-toolchain.toml ./
COPY rust ./rust
COPY vendor ./vendor
RUN cargo build --release --bin forum-fetch


FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 linuxdo \
    && useradd --uid 10001 --gid 10001 --create-home linuxdo \
    && install -d -m 0700 -o linuxdo -g linuxdo /var/lib/linuxdo

COPY --from=fetch-builder /src/target/release/forum-fetch /usr/local/bin/forum-fetch
# The profile names its recorded answers relative to its own directory, so both live here.
COPY vendor/cfsolve/profiles /usr/local/share/cfsolve
COPY linuxdo ./linuxdo
COPY extension ./extension
USER 10001:10001
EXPOSE 8765
CMD ["uvicorn", "linuxdo.server:app", "--host", "0.0.0.0", "--port", "8765", "--no-access-log", "--timeout-graceful-shutdown", "10"]

FROM runtime AS test
COPY tests ./tests
CMD ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
