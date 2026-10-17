FROM python:3.12-slim-bookworm

WORKDIR /workspace

COPY pyproject.toml README.md ./
COPY src/ src/
RUN python -m pip install --no-cache-dir ".[builder]"

COPY sites/ sites/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEBANYTHING_ROOT=/workspace \
    WEBANYTHING_SITES_ROOT=/workspace/sites \
    WEBANYTHING_SITE=uswds-docs-demo \
    WEBANYTHING_HOST=0.0.0.0 \
    WEBANYTHING_PORT=8000

EXPOSE 8000

ENTRYPOINT ["python", "-m", "webanything.runtime"]
CMD ["serve"]
