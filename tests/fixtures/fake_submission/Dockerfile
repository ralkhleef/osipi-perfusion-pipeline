# Fake submission Dockerfile for integration testing.
# Provides Python 3.11 with numpy so run.py can create a fake output file.
FROM python:3.11-slim

RUN pip install --no-cache-dir numpy

WORKDIR /submission
