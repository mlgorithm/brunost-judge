FROM python:3.13-slim
WORKDIR /app
COPY . .
# The API image is also the production control-plane image.  Production uses
# PostgreSQL (and may use S3-compatible artifact storage), so the image must
# include the production extras rather than only the HTTP server dependencies.
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir ".[production]"
ENTRYPOINT ["brunost"]
