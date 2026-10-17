FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home tracker \
    && mkdir -p /data /app/server/instance \
    && chown -R tracker:tracker /data /app
COPY --chown=tracker:tracker server ./server
COPY --chown=tracker:tracker examples ./examples
USER tracker
WORKDIR /app/server
ENV DATABASE_PATH=/data/experiments.sqlite3
EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "2", "--timeout", "60", "app:create_app()"]
