# Stage 1: build the React app. Runs on the server box; the node heap is capped
# so the build fits the 1 GB machine (swap covers the rest).
FROM node:22-alpine AS build

WORKDIR /app
# package-lock.json is required (npm ci). Generate it once, see ../README.md.
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ENV NODE_OPTIONS=--max-old-space-size=512
RUN npm run build

# Stage 2: Caddy serves the built app and proxies /api/*. The Caddyfile is
# bind-mounted by docker-compose.yml, so it is not copied here.
FROM caddy:2-alpine

COPY --from=build /app/dist /srv
