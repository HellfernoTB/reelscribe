"""Shared fixtures: a throwaway PostgreSQL schema per test.

Tests that use ``pg_url`` skip unless ``HOMELAB_TEST_DATABASE_URL`` is set.
"""

import os
import uuid
from urllib.parse import quote

import psycopg
import pytest


@pytest.fixture
def pg_url():
    base = os.environ.get("HOMELAB_TEST_DATABASE_URL")
    if not base:
        pytest.skip("HOMELAB_TEST_DATABASE_URL not set")
    schema = f"reelscribe_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        sep = "&" if "?" in base else "?"
        url = f"{base}{sep}options={quote(f'-c search_path={schema}')}"
        try:
            yield url
        finally:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
