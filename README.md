# Soulseek Audiobook Service

Containerized FastAPI service for SAS, published through GitHub Container Registry. SAS acts as the intermediary between Audiobook Request and slskd by picking up the ABR API request and querying slskd for the audiobook. The application is designed to score potential matches
and rank them, with single file M4B matches scoring the highest.
Lots of AI used, almost exclusively. Use at your own risk.

## Security

Pull and run

```bash
docker pull ghcr.io//DaisyAge12/soulseek-audiobook-service:latest
cp .env.example .env
# Edit .env and set SAS_IMAGE to the published image.
docker compose up -d
```

