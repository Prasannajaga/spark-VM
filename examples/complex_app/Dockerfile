FROM python:3.12-slim
WORKDIR /workspace
COPY examples/complex_app /workspace
RUN pip install --no-cache-dir -r requirements.txt
ENV PYTHONPATH=/workspace/src
CMD ["sh", "-lc", "pytest -q && python src/app/main.py"]
