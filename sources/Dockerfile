FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml ./
COPY wizard_sources ./wizard_sources
RUN uv pip install --system --no-cache .
EXPOSE 8081
CMD ["wizard-sources", "api", "--port", "8081"]
