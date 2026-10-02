FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt.
RUN pip install --no-cache-dir -r requirements.txt
COPY..
RUN mkdir -p logs workspace.rem_snapshots
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import httpx; httpx.get('http://localhost:8080/health', timeout=2)"
CMD ["python", "main.py"]
