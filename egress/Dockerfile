FROM python:3.12-slim

WORKDIR /app
COPY app.py .

# Point the workload at the in-enclave validating resolver (unbound on
# loopback). The runtime exposes 127.0.0.1:53 inside the netns the
# workload runs in. Without this, libc reads the base image's default
# resolv.conf (often public IPs that the egress allowlist will deny)
# and hostname lookups fall over before the request even goes out.
RUN echo "nameserver 127.0.0.1" > /etc/resolv.conf

EXPOSE 8080
CMD ["python", "-u", "app.py"]
