# Build context: repository root.  docker build -f apps/web/Dockerfile .
FROM node:24.22.0-alpine3.24@sha256:44fce696f38ba8c987c4db89954051000bbd05ffd8d8659ba15ed4a8602e937f AS build
WORKDIR /src
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci
COPY apps/web/index.html apps/web/tsconfig.json apps/web/vite.config.ts ./
COPY apps/web/src ./src
# Public, non-secret build metadata only: it ends up in the bundle as resource attributes.
ARG GIT_SHA=dev
ARG DEMO_ENVIRONMENT=local
RUN VITE_SERVICE_VERSION="$GIT_SHA" VITE_DEPLOYMENT_ENVIRONMENT="$DEMO_ENVIRONMENT" npm run build

FROM nginx:1.31.7-alpine@sha256:42c36dfd0b3503b7966771550a91521262b4750665929821abf5069715610e20
COPY infra/gateway/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html
EXPOSE 8080
