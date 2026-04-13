FROM python:3.11-slim-bookworm

# Install BlueZ, D-Bus, and build dependencies for PyGObject / dbus-python
# - bluez/bluetooth: BlueZ runtime + utilities (bluetoothctl, hciconfig, ...)
# - dbus + libdbus-1-dev / libdbus-glib-1-dev: D-Bus runtime + headers for dbus-python
# - libglib2.0-dev / libgirepository1.0-dev / gir1.2-glib-2.0: GObject Introspection bits
#   for PyGObject 3.50 (which uses Meson/Ninja and pulls in pycairo, requiring libcairo2-dev)
# - libbluetooth-dev: BlueZ headers (used by some Python BT bindings)
# - meson, ninja-build, build-essential, gcc, make, pkg-config, python3-dev: build toolchain
RUN apt-get update && apt-get install -y --no-install-recommends \
        bluez \
        bluetooth \
        dbus \
        libbluetooth-dev \
        libdbus-1-dev \
        libdbus-glib-1-dev \
        libglib2.0-dev \
        libgirepository1.0-dev \
        gir1.2-glib-2.0 \
        libcairo2-dev \
        build-essential \
        gcc \
        make \
        meson \
        ninja-build \
        pkg-config \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

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
