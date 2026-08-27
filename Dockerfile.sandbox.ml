# ML contest runtime. Keep this image digest-pinned in production and map it
# to the python-3.13-ml-v1 task runtime through BRUNOST_JUDGE_SANDBOX_IMAGES.
ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}

RUN apt-get update \
    && apt-get install --no-install-recommends -y bubblewrap gcc g++ rustc \
    && rm -rf /var/lib/apt/lists/*

# These are the portable CPU packages promised by python-3.13-ml-v1. Heavy
# frameworks such as PyTorch belong in a separately versioned runtime image.
RUN python -m pip install --no-cache-dir --only-binary=:all: \
    numpy pandas scikit-learn pyarrow

COPY src/grader /opt/brunost/grader
COPY src/brunost_judge/artifacts.py /opt/brunost/artifacts.py
COPY src/brunost_judge/plugins.py /opt/brunost/brunost_judge/plugins.py
ENV PYTHONPATH=/opt/brunost
WORKDIR /workspace
