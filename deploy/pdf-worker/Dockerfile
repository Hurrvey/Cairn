FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 libgl1 tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim tesseract-ocr-chi-tra && rm -rf /var/lib/apt/lists/*
COPY deploy/pdf-worker/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock
COPY deploy/pdf-worker/requirements-ocr.txt /tmp/requirements-ocr.txt
RUN pip install --no-cache-dir -r /tmp/requirements-ocr.txt
RUN useradd --create-home --uid 10001 pdfworker
COPY src/cairn/ingestion/docling_worker.py /opt/docling_worker.py
COPY src/cairn/core/ocr_environment.py /opt/ocr_environment.py
USER 10001
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 OMP_NUM_THREADS=2
ENTRYPOINT ["python", "/opt/docling_worker.py"]
