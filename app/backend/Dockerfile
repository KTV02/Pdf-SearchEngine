FROM docker.io/library/python@sha256:a4ccdf11e73bd3f74e07f95e63f1b003467bb16cb77e942ddf45476c2cb8467c

# Install system dependencies
RUN apk add --no-cache poppler-utils tesseract-ocr tesseract-ocr-data-deu tesseract-ocr-data-eng tesseract-ocr-data-osd ghostscript qpdf glib libsm libxext libxrender curl iproute2 fontconfig font-dejavu libstdc++ libgcc

# Set work directory
WORKDIR /app

# Copy code
COPY . /app

# Install Python dependencies
RUN python -m pip install --no-cache-dir pip==26.2.1 setuptools==79.0.1 wheel==0.46.3
RUN python -m pip install --no-cache-dir --no-build-isolation --only-binary=lxml,pikepdf,pillow,psutil,pycryptodome,MarkupSafe,charset-normalizer -r requirements.security.lock

# Create folders
RUN mkdir -p ocr/converted/uploads index_folder tmp

# Expose Flask port
ENV PDFSEARCH_BIND_ADDRESS=0.0.0.0
EXPOSE 5050

# Run the app
CMD ["python", "app.py"]