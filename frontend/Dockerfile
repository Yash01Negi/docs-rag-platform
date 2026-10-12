# syntax=docker/dockerfile:1
# Development image: runs the Vite dev server. A production image (static build behind a
# web server) will be added with the deployment work in a later phase.
FROM node:22-alpine

WORKDIR /app
# Vite writes temporary files next to its config and in node_modules/.vite,
# so the unprivileged user must own the app directory.
RUN chown node:node /app
USER node

COPY --chown=node:node package.json package-lock.json ./
RUN npm ci

COPY --chown=node:node . .

EXPOSE 5173

CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
