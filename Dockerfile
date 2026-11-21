FROM python:3.13-alpine
WORKDIR /app
COPY app.py /app/app.py
ENV DATA_DIR=/data \
    PORT=8090 \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8090/healthz',timeout=4)" || exit 1
CMD ["python", "-u", "/app/app.py"]
