FROM python:3.11-slim

WORKDIR /submission

# Copy submission files into image
COPY . .

# No extra dependencies — run.py uses stdlib only
CMD ["python3", "run.py"]
