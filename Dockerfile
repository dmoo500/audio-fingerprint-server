FROM python:3.14-slim
WORKDIR /opt/fingerprint
COPY music-assistant-fingerprint-server/pyproject.toml ./
COPY music-assistant-fingerprint-server/fingerprint_server ./fingerprint_server
ARG FP_INSTALL_EXTRAS=postgres
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir ".[${FP_INSTALL_EXTRAS}]"
RUN useradd --uid 10001 --create-home fingerprint && mkdir /data && chown fingerprint /data
USER fingerprint
ENV FP_DATABASE=/data/fingerprints.db
EXPOSE 8097
CMD ["uvicorn", "fingerprint_server.app:app", "--host", "0.0.0.0", "--port", "8097", "--workers", "1", "--log-config", "fingerprint_server/logging.json"]
