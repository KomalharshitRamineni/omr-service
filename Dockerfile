FROM python:3.12-slim

# pdfinfo and pdftoppm (poppler) render PDF pages; they run as separate programs
RUN apt-get update \
 && apt-get install -y --no-install-recommends poppler-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# Download homr's models at build time so nothing is fetched while serving
# (rapidocr's title-detection models ship inside its package).
RUN pip install --no-cache-dir -r requirements.txt \
 && homr --init

RUN useradd --create-home omr
USER omr
COPY omr_service ./omr_service

ENV WORK_DIR=/tmp
EXPOSE 8000
# the API; the worker runs the same image with: python -m omr_service.worker
CMD ["uvicorn", "omr_service.app:app", "--host", "0.0.0.0", "--port", "8000"]
