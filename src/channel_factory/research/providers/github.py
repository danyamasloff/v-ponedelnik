"""GitHub Releases through the official REST API."""

from __future__ import annotations

import os
from datetime import datetime

import httpx

from channel_factory.core.enums import ResearchProviderType
from channel_factory.research.providers.base import (
    DEFAULT_TIMEOUT_SECONDS,
    EXCERPT_LIMIT,
    USER_AGENT,
    ProviderError,
    RawResearchItem,
    SourceConfig,
)

API_ROOT = "https://api.github.com"
DEFAULT_PER_PAGE = 20


class GitHubReleasesProvider:
    """Reads release notes from a repository.

    A token is optional but strongly recommended: unauthenticated requests get
    a much smaller hourly quota, which a scheduled poller exhausts quickly.
    """

    provider_type = ResearchProviderType.GITHUB_RELEASES

    def __init__(self, token: str | None = None) -> None:
        self._token = token or os.environ.get("GITHUB_TOKEN")

    async def fetch(self, source: SourceConfig) -> list[RawResearchItem]:
        repo = source.repo or (source.config or {}).get("repo")
        if not repo:
            raise ProviderError(f"source {source.key!r} has no repo")

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        per_page = int((source.config or {}).get("per_page", DEFAULT_PER_PAGE))
        url = f"{API_ROOT}/repos/{repo}/releases"

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            try:
                response = await client.get(url, headers=headers, params={"per_page": per_page})
            except httpx.HTTPError as exc:
                raise ProviderError(f"github request failed for {repo}: {exc}") from exc

        if response.status_code == 403 and "rate limit" in response.text.lower():
            raise ProviderError(
                f"github rate limit reached for {repo}; set GITHUB_TOKEN to raise the quota"
            )
        if response.status_code >= 400:
            raise ProviderError(f"github returned HTTP {response.status_code} for {repo}")

        items: list[RawResearchItem] = []
        for release in response.json():
            if release.get("draft"):
                continue
            body = (release.get("body") or "").strip()
            published = release.get("published_at") or release.get("created_at")
            items.append(
                RawResearchItem(
                    external_id=str(release["id"]),
                    url=release.get("html_url") or f"https://github.com/{repo}/releases",
                    title=(release.get("name") or release.get("tag_name") or repo)[:1024],
                    summary=body[:2000] or None,
                    content_excerpt=body[:EXCERPT_LIMIT] or None,
                    published_at=datetime.fromisoformat(published) if published else None,
                    author=(release.get("author") or {}).get("login"),
                    publisher=repo,
                    language="en",
                    raw_metadata={
                        "tag_name": release.get("tag_name"),
                        "prerelease": release.get("prerelease", False),
                        "repo": repo,
                    },
                )
            )
        return items
