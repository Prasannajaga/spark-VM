FROM python:3.12-slim
WORKDIR /workspace
COPY examples/simple_app /workspace
CMD ["python", "m.py"]
