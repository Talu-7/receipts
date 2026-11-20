# Image for the API and the worker (same image, different command). Build context: the repository root.
#   docker build -f service/Dockerfile -t receipts-service .
FROM docker:27-cli AS dockercli

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# The worker drives the host's Docker daemon (socket mounted by compose) to start sandbox containers.
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
COPY service/requirements.txt service/requirements.txt
RUN pip install --no-cache-dir -r service/requirements.txt
COPY receipts receipts
COPY service service
ENV SERVICE_DATA_DIR=/data
EXPOSE 8000
CMD ["uvicorn", "--factory", "service.app:create_app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
