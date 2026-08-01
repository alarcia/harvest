FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

ENV PATH="/app/.venv/bin:$PATH"

# A build has no environment, and settings.py now refuses to start without a
# real DJANGO_SECRET_KEY. Hand collectstatic a throwaway one, scoped to this
# single command: it only walks the static files, never signs anything, and
# the value never reaches a running container.
RUN DJANGO_SECRET_KEY=build-time-collectstatic-only python manage.py collectstatic --noinput

EXPOSE 8000

CMD ["sh", "-c", "python manage.py migrate --noinput && gunicorn harvest.wsgi:application --bind 0.0.0.0:8000"]
