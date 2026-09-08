# Soulseek Audiobook Service

Containerized FastAPI service for SAS, published through GitHub Container Registry. SAS acts as the intermediary between Audiobook Request and slskd by picking up the ABR API request and querying slskd for the audiobook. The application is designed to score potential matches
and rank them, with single file M4B matches scoring the highest.
Lots of AI used, almost exclusively. Use at your own risk.
## Security

This repository must not contain real API keys, JWT secrets, passwords, private keys, populated `.env` files, databases, or logs. Copy `.env.example` to `.env` and set credentials only on the deployment host.

If a genuine credential has ever been committed, revoke and rotate it. Deleting it from the current file does not remove it from Git history.

## Repository structure

Place the complete Python package under `app/`. The container starts `app.main:app` and checks `GET /health` on port 8099.

## Local build

```bash
cp .env.example .env
# Edit .env locally.
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

Check the service:

```bash
curl http://localhost:8099/health
docker compose logs -f sas
```

## Pull and run

```bash
docker pull ghcr.io/<owner>/<repository>:latest
cp .env.example .env
# Edit .env and set SAS_IMAGE to the published image.
docker compose up -d
```


## Required application modules

The supplied `main.py` imports `abr`, `abr_worker`, `config`, `database`, `duplicate`, `schemas`, `scoring`, `slskd`, and `worker`. Those modules must exist under `app/` before the image can build and start successfully.

## Environment-variable note

The exact accepted settings are defined by `app/config.py`, which was not supplied in the reviewed files. Verify `.env.example` against that module before deployment, especially the SLSKD, ABR, duplicate-detection, and path setting names.
