FROM python:3.11-slim-bookworm

# Install BlueZ, D-Bus, and build dependencies for PyGObject / dbus-python.
# libcairo2-dev is required because PyGObject pulls pycairo in as a build dep.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bluez \
        dbus \
        libdbus-1-dev \
        libglib2.0-dev \
        libgirepository1.0-dev \
        libcairo2-dev \
        gcc \
        pkg-config \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY bt_gateway/ bt_gateway/
COPY run.py .

# Persistent config volume
VOLUME ["/data"]

# Web interface port
EXPOSE 8080

ENV PYTHONUNBUFFERED=1
ENV CONFIG_PATH=/data/config.json
ENV LOG_LEVEL=INFO

CMD ["python", "run.py"]
