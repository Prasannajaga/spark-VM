FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates iproute2 \
 && python -m pip install --no-cache-dir requests \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
COPY examples/network-app /workspace

CMD ["python", "-u", "app.py"]
