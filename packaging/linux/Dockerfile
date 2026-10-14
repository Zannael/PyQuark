FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip libpython3.10 git curl ca-certificates file \
    build-essential zsync libusb-1.0-0 libgl1 libegl1 libdbus-1-3 \
    libxkbcommon0 libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 \
    libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-xinerama0 \
    libxcb-randr0 libxcb-shape0 xvfb xauth x11-utils imagemagick \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/pyquark-build
# Qt 6.7 wheels retain a glibc 2.28 baseline; newer wheels can raise it.
RUN /opt/pyquark-build/bin/pip install --no-cache-dir \
    PyQt6==6.7.1 PyQt6-Qt6==6.7.3 PyQt6-sip==13.10.2 \
    pyusb==1.3.1 rarfile==4.2 multivolumefile==0.2.3 pyinstaller==6.20.0
WORKDIR /workspace
