FROM python:3.12-slim

# Baked into the image so v1 and v2 are distinct images with distinct PCRs.
# Build v2 with: docker build --build-arg APP_VERSION=2 -t upgrades:v2 .
ARG APP_VERSION=1
ENV APP_VERSION=${APP_VERSION}

WORKDIR /app
COPY app.py .

EXPOSE 8080
CMD ["python", "-u", "app.py"]
