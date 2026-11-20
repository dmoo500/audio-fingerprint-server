FROM node:24-slim AS recognition-frontend
WORKDIR /frontend
RUN corepack enable
COPY music-assistant-fingerprint-server/recognition-frontend/package.json music-assistant-fingerprint-server/recognition-frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY music-assistant-fingerprint-server/recognition-frontend ./
COPY music-assistant-fingerprint-server/frontend-shared /frontend-shared
COPY music-assistant-fingerprint-server/fingerprint_server/recognition/static/recording.js /fingerprint_server/recognition/static/recording.js
RUN pnpm build

FROM node:24-slim AS admin-frontend
WORKDIR /frontend
RUN corepack enable
COPY music-assistant-fingerprint-server/admin-frontend/package.json music-assistant-fingerprint-server/admin-frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY music-assistant-fingerprint-server/admin-frontend ./
COPY music-assistant-fingerprint-server/frontend-shared /frontend-shared
RUN pnpm build

FROM python:3.14-slim
WORKDIR /opt/fingerprint
COPY music-assistant-fingerprint-server/pyproject.toml ./
COPY music-assistant-fingerprint-server/fingerprint_server ./fingerprint_server
COPY --from=recognition-frontend /fingerprint_server/recognition/dist ./fingerprint_server/recognition/dist
COPY --from=admin-frontend /fingerprint_server/admin/dist ./fingerprint_server/admin/dist
ARG FP_INSTALL_EXTRAS=postgres
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir ".[${FP_INSTALL_EXTRAS}]"
RUN useradd --uid 10001 --create-home fingerprint \
    && mkdir /data /recognition-data && chown fingerprint /data /recognition-data
USER fingerprint
ENV FP_DATABASE=/data/fingerprints.db
EXPOSE 8097
CMD ["python", "-m", "fingerprint_server.aiohttp_runner", "fingerprint_server.app", "--host", "0.0.0.0", "--port", "8097"]
