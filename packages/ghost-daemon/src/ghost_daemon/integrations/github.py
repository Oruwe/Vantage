from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.github.com"


class GithubClient:
    """Minimal async GitHub REST client using httpx."""

    def __init__(self, token: str, default_repo: str = "") -> None:
        self._token = token
        self._default_repo = default_repo  # "owner/repo"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _parse_repo(self, repo: str) -> tuple[str, str]:
        repo = repo or self._default_repo
        if "/" not in repo:
            raise ValueError(f"Repo must be 'owner/repo', got: {repo!r}")
        owner, name = repo.split("/", 1)
        return owner, name

    async def find_pr_for_branch(self, branch: str, repo: str = "") -> dict[str, Any] | None:
        """Find the open PR whose head is the given branch. Returns the PR dict or None."""
        owner, name = self._parse_repo(repo)
        url = f"{_BASE}/repos/{owner}/{name}/pulls"
        params = {"head": f"{owner}:{branch}", "state": "open", "per_page": 5}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=self._headers(), params=params)
        if not resp.is_success:
            logger.warning("GitHub find_pr_for_branch: %d %s", resp.status_code, resp.text[:200])
            return None
        prs = resp.json()
        return prs[0] if prs else None

    async def update_pr_body(self, pr_number: int, body: str, repo: str = "") -> bool:
        """Replace the PR description. Returns True on HTTP 200."""
        owner, name = self._parse_repo(repo)
        url = f"{_BASE}/repos/{owner}/{name}/pulls/{pr_number}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.patch(url, headers=self._headers(), json={"body": body})
        if resp.is_success:
            logger.info("Updated PR #%d body in %s/%s", pr_number, owner, name)
            return True
        logger.warning("GitHub update_pr_body: %d %s", resp.status_code, resp.text[:200])
        return False

    async def fetch_pr_reviews(self, pr_number: int, repo: str = "") -> list[dict[str, Any]]:
        """Return all review objects for a PR (state, body, reviewer login)."""
        owner, name = self._parse_repo(repo)
        url = f"{_BASE}/repos/{owner}/{name}/pulls/{pr_number}/reviews"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=self._headers())
        if not resp.is_success:
            logger.warning("GitHub fetch_pr_reviews: %d %s", resp.status_code, resp.text[:200])
            return []
        return resp.json()

    async def fetch_pr_review_comments(self, pr_number: int, repo: str = "") -> list[dict[str, Any]]:
        """Return all inline review comments for a PR."""
        owner, name = self._parse_repo(repo)
        url = f"{_BASE}/repos/{owner}/{name}/pulls/{pr_number}/comments"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=self._headers(), params={"per_page": 100})
        if not resp.is_success:
            logger.warning("GitHub fetch_pr_review_comments: %d %s", resp.status_code, resp.text[:200])
            return []
        return resp.json()

    async def list_open_prs(self, repo: str = "") -> list[dict[str, Any]]:
        """Return all open PRs (up to 30)."""
        owner, name = self._parse_repo(repo)
        url = f"{_BASE}/repos/{owner}/{name}/pulls"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=self._headers(), params={"state": "open", "per_page": 30})
        if not resp.is_success:
            return []
        return resp.json()
