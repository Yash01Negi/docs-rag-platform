# Local S3 emulator for the optional "s3-local" Compose profile: moto's standalone server,
# which speaks the S3 API over HTTP. For local testing only; it keeps objects in memory.
FROM python:3.12-slim
RUN pip install --no-cache-dir "moto[server]==5.2.4" \
    && useradd --create-home --uid 10002 emulator
USER emulator
EXPOSE 9000
CMD ["moto_server", "-H", "0.0.0.0", "-p", "9000"]
