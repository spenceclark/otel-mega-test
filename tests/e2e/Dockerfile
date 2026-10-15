# Build context: tests/e2e.  Run with BASE_URL=http://web:8080 and EVIDENCE_DIR=/evidence (mounted).
FROM mcr.microsoft.com/playwright:v1.64.1-noble@sha256:7852dc5a7d72f5fe458a597d690f6c42fbc8e9e26ddfa95f8e6ef552558d9bbf
WORKDIR /e2e
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json playwright.config.ts support.ts ask.spec.ts concurrency.spec.ts ./
CMD ["npx", "playwright", "test"]
