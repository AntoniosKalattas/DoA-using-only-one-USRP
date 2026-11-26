# Docker image for the Capture Console

Build (from the Thesis folder):
```
docker build --platform linux/amd64 -f docker/Dockerfile -t usrp-console:latest .
```
Run with `docker compose -f docker/docker-compose.yml up -d` (add `--profile ngrok` + `docker/.env`
for the phone link). Only code goes into the image (see `Dockerfile.dockerignore`).

Export a Windows bundle: `docker save usrp-console:latest | gzip -1 > usrp-console-image.tar.gz`
and ship it with the files in `~/Downloads/usrp-console-windows/` (start.bat, attach-b210.ps1, README).
