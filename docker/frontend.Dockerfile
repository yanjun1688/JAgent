# syntax=docker/dockerfile:1
# Harness frontend — build with Vite, serve static assets via nginx (reverse-proxies /api).
FROM node:22-alpine AS build

WORKDIR /app

COPY frontend/package.json frontend/package-lock.json ./
# package-lock.json is out of sync with package.json (esbuild tree), so `npm ci`
# rejects it. `npm install` resolves in-container instead.
RUN npm install --no-audit --no-fund

COPY frontend/ ./
RUN npm run build

FROM nginx:alpine

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
