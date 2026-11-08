# Collaboration service image. Build from the REPOSITORY ROOT so the npm workspace (one hoisted
# install of Tiptap/ProseMirror/Yjs) and the shared schema package are available:
#   docker build -f collab/Dockerfile -t threadforge-collab .
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
COPY packages/editor-schema/package.json packages/editor-schema/
COPY collab/package.json collab/
# the frontend workspace is not needed here; npm still wants its manifest to resolve the lockfile
COPY frontend/package.json frontend/
RUN npm ci --workspace @threadforge/collab --workspace @threadforge/editor-schema --include-workspace-root
COPY packages/editor-schema packages/editor-schema
COPY collab collab
RUN npm run build -w @threadforge/editor-schema && npm run build -w @threadforge/collab
RUN npm prune --omit=dev --workspace @threadforge/collab --workspace @threadforge/editor-schema

FROM node:22-alpine
ENV NODE_ENV=production
WORKDIR /app
COPY --from=build /app/package.json ./
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/packages/editor-schema/package.json packages/editor-schema/package.json
COPY --from=build /app/packages/editor-schema/dist packages/editor-schema/dist
COPY --from=build /app/collab/package.json collab/package.json
COPY --from=build /app/collab/dist collab/dist
USER node
WORKDIR /app/collab
EXPOSE 4010
HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD wget -qO- http://127.0.0.1:4010/health || exit 1
CMD ["node", "dist/main.js"]
