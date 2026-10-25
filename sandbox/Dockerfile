# Receipts sandbox image: what each Nebius Serverless Job runs.
#   docker build --platform linux/amd64 -t <you>/receipts-sandbox:latest -f sandbox/Dockerfile .
#   docker push <you>/receipts-sandbox:latest      (the image must be PUBLIC)
# Then pin by digest in RECEIPTS_JOB_IMAGE=ghcr.io/<you>/receipts-sandbox@sha256:... so receipts are exact.
FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime

RUN apt-get update && apt-get install -y --no-install-recommends git util-linux curl wget \
    && rm -rf /var/lib/apt/lists/*

# Common scientific stack pre-baked so jobs start fast (scripts can still pip install more).
RUN pip install --no-cache-dir numpy scipy pandas scikit-learn transformers datasets accelerate

COPY receipts/sandbox_entry.py /opt/receipts/sandbox_entry.py

# The job's --args is the base64 payload; it becomes $1 of the entrypoint.
ENTRYPOINT ["python", "/opt/receipts/sandbox_entry.py"]
