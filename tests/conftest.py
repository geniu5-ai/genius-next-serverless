"""Configuracao minima para os testes rodarem sem banco nem rede."""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("HMAC_SECRET", "test-secret")
