FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY router ./router
RUN python -m pip install --no-cache-dir .
RUN mkdir -p /data
ENV PYTHONUNBUFFERED=1
ENV ROUTER_DB_FILE=/data/router.db
ENV ROUTER_CATALOG_FILE=/data/router-catalog.json
VOLUME ["/data"]
EXPOSE 8010
CMD ["uvicorn", "router.api:app", "--host", "0.0.0.0", "--port", "8010"]
