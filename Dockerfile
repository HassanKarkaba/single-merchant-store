FROM node:24.22.1-bookworm-slim@sha256:be8204d4bd327363357f532187b752ff62759c31bedf98e9e18fc08c9cbd605c AS browser
WORKDIR /opt/store
COPY package.json package-lock.json ./
RUN npm install --global npm@10.2.4 --no-audit --no-fund \
 && npm ci --ignore-scripts --no-audit --no-fund
COPY app/controllers/browser ./app/controllers/browser
COPY app/views ./app/views
COPY config/tsconfig.json config/vite.client.config.ts config/vite.server.config.ts ./config/
RUN npm run check && npm run build

FROM postgres:16.15-bookworm@sha256:e8a07d2c7579b038938cdba6c539e580e5001caf9ea203d397b5cc83e76bd938 AS postgres
FROM caddy:2.11.7-alpine@sha256:5e5286cb37d44086d651a5363f05f43a692ce4d649d88fb1bd844579f45fa9e8 AS caddy
FROM python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HOME=/state LANG=C.UTF-8 LC_ALL=C.UTF-8 SME_CONTAINER_RUNTIME=1 \
    PATH=/usr/lib/postgresql/16/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin
RUN apt-get update \
 && apt-get install --yes --no-install-recommends \
      ca-certificates libstdc++6 libicu72 libldap-2.5-0 liblz4-1 libreadline8 \
      libssl3 libxml2 libxslt1.1 libzstd1 zlib1g libgssapi-krb5-2 libpam0g \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 store \
 && useradd --uid 10001 --gid store --home-dir /state --no-create-home --shell /usr/sbin/nologin store \
 && install -d -o store -g store -m 0700 /state
COPY --from=postgres /usr/lib/postgresql/16 /usr/lib/postgresql/16
COPY --from=postgres /usr/share/postgresql /usr/share/postgresql
COPY --from=postgres /usr/lib/*-linux-gnu/libpq.so* /usr/local/lib/
COPY --from=browser /usr/local/bin/node /usr/local/bin/node
COPY --from=browser /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
COPY --from=caddy /usr/bin/caddy /usr/local/bin/caddy
RUN python -c "import os; p='/usr/local/bin/caddy'; os.removexattr(p, 'security.capability') if 'security.capability' in os.listxattr(p) else None"
RUN ldconfig \
 && ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && python --version | grep -Fx 'Python 3.12.15' \
 && postgres --version | grep -E 'PostgreSQL\) 16\.15' \
 && node --version | grep -Fx 'v24.22.1' \
 && NPM_CONFIG_CACHE=/tmp/store-build-npm-cache npm --version | grep -Fx '10.2.4' \
 && caddy version | grep -E '^v2\.11\.7( |$)' \
 && ! ldd /usr/lib/postgresql/16/bin/postgres | grep 'not found'
RUN rm -rf /tmp/store-build-npm-cache \
 && test -z "$(ls -A /state)"
WORKDIR /opt/store
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --disable-pip-version-check --require-hashes -r requirements.lock
COPY package.json package-lock.json ./
COPY --from=browser /opt/store/node_modules/react ./node_modules/react
COPY --from=browser /opt/store/node_modules/react-dom ./node_modules/react-dom
COPY --from=browser /opt/store/node_modules/scheduler ./node_modules/scheduler
COPY --from=browser /opt/store/dist ./dist
COPY app ./app
COPY config ./config
COPY manage.py ./
RUN test -z "$(ls -A /state)"
USER store
VOLUME ["/state"]
EXPOSE 8443
HEALTHCHECK --interval=5s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python", "-m", "app.controllers.container_boot", "--healthcheck"]
ENTRYPOINT ["python", "-m", "app.controllers.container_boot"]
