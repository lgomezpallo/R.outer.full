FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY router ./router
# BuildKit mounts the local wheelhouse without baking wheels into the image.
# CI sets ROUTER_OFFLINE_WHEELS=1 and --network none; ordinary builds stay compatible.
ARG ROUTER_OFFLINE_WHEELS=0
RUN --mount=type=bind,source=wheelhouse,target=/opt/router-wheels,ro if [ "$ROUTER_OFFLINE_WHEELS" = "1" ]; then python -m pip install --no-index --find-links=/opt/router-wheels .; else python -m pip install --no-cache-dir .; fi
RUN mkdir -p /data
ENV PYTHONUNBUFFERED=1
ENV ROUTER_DB_FILE=/data/router.db
ENV ROUTER_CATALOG_FILE=/data/router-catalog.json
VOLUME ["/data"]
EXPOSE 8010
CMD ["uvicorn", "router.api:app", "--host", "0.0.0.0", "--port", "8010"]
