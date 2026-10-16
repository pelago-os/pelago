"""Run the public build using its explicit environment configuration."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings


def main():
    import uvicorn
    uvicorn.run('app.main:app', host=settings.host, port=settings.port, proxy_headers=False)


if __name__ == '__main__':
    main()
