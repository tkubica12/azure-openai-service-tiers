FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=https://packagefeedproxy.microsoft.io/pypi/simple/
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
RUN useradd --uid 10001 --create-home worker
USER 10001
CMD ["python", "-m", "app.main"]
