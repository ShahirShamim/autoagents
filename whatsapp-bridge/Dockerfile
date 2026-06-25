FROM node:22-bookworm-slim

WORKDIR /app
ENV NODE_ENV=production PORT=8080

COPY package.json ./
RUN npm install --omit=dev

COPY index.js ./

EXPOSE 8080
CMD ["node", "index.js"]
