FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
RUN apt-get update && apt-get install -y --no-install-recommends libsndfile1 espeak-ng && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY server.py .
USER 1000:1000
ENV OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 HF_HUB_OFFLINE=1 NUMBA_CACHE_DIR=/tmp/numba
EXPOSE 18835
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "18835", "--no-access-log"]
