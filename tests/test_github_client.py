from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ghost_daemon.integrations.github import GithubClient


def _mock_response(data, status=200):
    resp = MagicMock()
    resp.is_success = (200 <= status < 300)
    resp.status_code = status
    resp.json = MagicMock(return_value=data)
    resp.text = json.dumps(data)
    return resp


def _mock_client(response):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.get = AsyncMock(return_value=response)
    client.patch = AsyncMock(return_value=response)
    return client


# ── find_pr_for_branch ────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_find_pr_returns_first_match():
    pr_data = [{"number": 42, "title": "Add auth", "head": {"ref": "feature/auth"}}]
    resp = _mock_response(pr_data)

    with patch("httpx.AsyncClient", return_value=_mock_client(resp)):
        client = GithubClient(token="ghp_fake", default_repo="owner/repo")
        result = await client.find_pr_for_branch("feature/auth")

    assert result is not None
    assert result["number"] == 42


@pytest.mark.anyio
async def test_find_pr_returns_none_when_empty():
    resp = _mock_response([])

    with patch("httpx.AsyncClient", return_value=_mock_client(resp)):
        client = GithubClient(token="ghp_fake", default_repo="owner/repo")
        result = await client.find_pr_for_branch("feature/gone")

    assert result is None


@pytest.mark.anyio
async def test_find_pr_returns_none_on_error():
    resp = _mock_response({"message": "Not Found"}, status=404)

    with patch("httpx.AsyncClient", return_value=_mock_client(resp)):
        client = GithubClient(token="ghp_fake", default_repo="owner/repo")
        result = await client.find_pr_for_branch("feature/x")

    assert result is None


# ── update_pr_body ────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_update_pr_body_success():
    resp = _mock_response({"number": 7, "body": "new body"})

    with patch("httpx.AsyncClient", return_value=_mock_client(resp)):
        client = GithubClient(token="ghp_fake", default_repo="owner/repo")
        ok = await client.update_pr_body(7, body="new body")

    assert ok is True


@pytest.mark.anyio
async def test_update_pr_body_failure():
    resp = _mock_response({"message": "Forbidden"}, status=403)

    with patch("httpx.AsyncClient", return_value=_mock_client(resp)):
        client = GithubClient(token="ghp_fake", default_repo="owner/repo")
        ok = await client.update_pr_body(7, body="new body")

    assert ok is False


# ── fetch_pr_reviews ──────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_fetch_pr_reviews_returns_list():
    reviews = [
        {"id": 1, "state": "APPROVED", "body": "LGTM", "user": {"login": "alice"}},
        {"id": 2, "state": "CHANGES_REQUESTED", "body": "Fix this", "user": {"login": "bob"}},
    ]
    resp = _mock_response(reviews)

    with patch("httpx.AsyncClient", return_value=_mock_client(resp)):
        client = GithubClient(token="ghp_fake", default_repo="owner/repo")
        result = await client.fetch_pr_reviews(42)

    assert len(result) == 2
    assert result[0]["state"] == "APPROVED"


@pytest.mark.anyio
async def test_fetch_pr_reviews_returns_empty_on_error():
    resp = _mock_response({"message": "Not Found"}, status=404)

    with patch("httpx.AsyncClient", return_value=_mock_client(resp)):
        client = GithubClient(token="ghp_fake", default_repo="owner/repo")
        result = await client.fetch_pr_reviews(99)

    assert result == []


# ── _parse_repo ───────────────────────────────────────────────────────────────

def test_parse_repo_from_default():
    client = GithubClient(token="x", default_repo="myorg/myrepo")
    owner, name = client._parse_repo("")
    assert owner == "myorg"
    assert name == "myrepo"


def test_parse_repo_override():
    client = GithubClient(token="x", default_repo="myorg/myrepo")
    owner, name = client._parse_repo("other/project")
    assert owner == "other"
    assert name == "project"


def test_parse_repo_raises_on_missing_slash():
    client = GithubClient(token="x", default_repo="")
    with pytest.raises(ValueError, match="owner/repo"):
        client._parse_repo("noslash")


# ── list_open_prs ─────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_list_open_prs():
    prs = [{"number": 1, "title": "PR 1"}, {"number": 2, "title": "PR 2"}]
    resp = _mock_response(prs)

    with patch("httpx.AsyncClient", return_value=_mock_client(resp)):
        client = GithubClient(token="ghp_fake", default_repo="owner/repo")
        result = await client.list_open_prs()

    assert len(result) == 2
    assert result[0]["number"] == 1
