FROM docker.io/library/python@sha256:16bf2b5c59a08523519d3c8589deca849285dee1d62a3653e124c3dba340f15e

WORKDIR /app

COPY . /app

RUN python -m pip install --no-cache-dir pip==26.2.1 setuptools==79.0.1 wheel==0.46.3
RUN python -m pip install --no-cache-dir --no-build-isolation -r requirements.security.lock

CMD ["python", "processor_api.py"]