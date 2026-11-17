FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir aioquic==1.8.0
COPY server.py /srv/server.py
ENTRYPOINT ["python", "/srv/server.py"]
