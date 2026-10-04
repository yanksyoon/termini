FROM python:3.12-slim

WORKDIR /app
COPY app/ ./app/

ENV TERMINI_HOST=0.0.0.0 \
    TERMINI_PORT=8080 \
    TERMINI_DB_PATH=/data/controller.db

VOLUME ["/data"]
EXPOSE 8080

CMD ["python3", "-m", "app.main"]
