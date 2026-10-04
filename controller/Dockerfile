FROM python:3.12-slim

# Build context is the repo root (see docker-compose.yml); the controller image
# carries both the controller app and the termini/ guardrail package it imports
# for the intent endpoints.
WORKDIR /app
COPY controller/app/ ./app/
COPY termini/ ./termini/

ENV PYTHONPATH=/app \
    TERMINI_HOST=0.0.0.0 \
    TERMINI_PORT=8080 \
    TERMINI_DB_PATH=/data/controller.db

VOLUME [ "/data" ]
EXPOSE 8080

CMD [ "python3", "-m", "app.main" ]
