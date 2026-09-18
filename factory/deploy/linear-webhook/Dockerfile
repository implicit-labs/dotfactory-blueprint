FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/factory/src PORT=8080
WORKDIR /app
COPY factory/src/dotfactory /app/factory/src/dotfactory
COPY factory/deploy/linear-webhook/start.py /app/start-receiver.py
RUN groupadd --gid 10001 factory && useradd --uid 10001 --gid 10001 --no-create-home factory \
    && mkdir /data && chown factory:factory /data && chmod 700 /data
USER 10001:10001
ENV LINEAR_WEBHOOK_DATABASE=/data/linear-agent-inbox.db
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/readyz', timeout=2).read()"
ENTRYPOINT ["python3", "/app/start-receiver.py"]
CMD ["python3", "-m", "dotfactory.agent_webhooks", "serve", "--host", "0.0.0.0"]
