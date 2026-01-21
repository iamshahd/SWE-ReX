#!/usr/bin/env bash

# Script to set up Apptainer test environment
# This pulls the necessary images and prepares the test environment

# bash strict mode
set -euo pipefail

echo "Setting up Apptainer test environment..."

# Check if Apptainer is installed
if ! command -v apptainer &> /dev/null; then
    echo "Error: Apptainer is not installed or not in PATH"
    echo "Please install Apptainer: https://apptainer.org/docs/admin/main/installation.html"
    exit 1
fi

echo "Apptainer version:"
apptainer --version

# Create cache directory for test images
CACHE_DIR="${APPTAINER_CACHEDIR:-$HOME/.apptainer/cache/test}"
mkdir -p "$CACHE_DIR"
echo "Using cache directory: $CACHE_DIR"

# Pull test images
echo "Pulling test images..."

# Python 3.11 image (used by most tests)
if [ ! -f "$CACHE_DIR/python_3.11-slim.sif" ]; then
    echo "Pulling python:3.11-slim..."
    apptainer pull "$CACHE_DIR/python_3.11-slim.sif" docker://python:3.11-slim
else
    echo "python:3.11-slim already exists in cache"
fi

# Python 3.11 full image
if [ ! -f "$CACHE_DIR/python_3.11.sif" ]; then
    echo "Pulling python:3.11..."
    apptainer pull "$CACHE_DIR/python_3.11.sif" docker://python:3.11
else
    echo "python:3.11 already exists in cache"
fi

# Ubuntu latest (for python standalone tests)
if [ ! -f "$CACHE_DIR/ubuntu_latest.sif" ]; then
    echo "Pulling ubuntu:latest..."
    apptainer pull "$CACHE_DIR/ubuntu_latest.sif" docker://ubuntu:latest
else
    echo "ubuntu:latest already exists in cache"
fi

echo ""
echo "Setup complete! You can now run Apptainer tests:"
echo "  pytest tests/test_apptainer_deployment.py -v"
echo ""
echo "To use the cached images in your tests, set:"
echo "  export APPTAINER_CACHEDIR=$CACHE_DIR"
