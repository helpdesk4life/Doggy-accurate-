FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Operator scripts. On the VPS these run inside this container — the host has no
# virtualenv, and create_admin.py / mint_invites.py import `app`. Without them in
# the image a fresh deployment has no operator account and no way to mint an
# invite code, so nobody can sign up. They are not served and not on the CMD path.
COPY scripts ./scripts

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
