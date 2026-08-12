FROM python:3.13-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir ".[server]"
ENTRYPOINT ["brunost"]
