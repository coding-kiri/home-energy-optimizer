FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py checkpoint.py data_source.py mqtt_client.py replay.py ./

# Non-root; SIGTERM from ECS reaches python directly (exec form, no shell).
RUN useradd -m producer
USER producer

ENTRYPOINT ["python", "replay.py"]
