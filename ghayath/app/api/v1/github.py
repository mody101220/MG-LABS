"""GitHub API (contract §14)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.core.envelope import build_success
from app.core.registry import register
from app.core.security import Principal, get_principal, require_mutating
from app.generated import models

router = APIRouter(tags=["GitHub"])


def _v(x):
    """Generated request fields may carry str defaults for enum types."""
    return x.value if hasattr(x, "value") and not isinstance(x, str) else x


@router.get("/integrations/github/repositories", operation_id="listGitHubRepositories", response_model=models.RepoListResponse)
async def list_github_repositories(request: Request,
                                   page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                                   principal: Principal = Depends(get_principal)):
    rows, _total = await request.app.state.services["github"].list_repos(page, page_size)
    return build_success(models.RepoListResponse, rows)


@router.get("/github/repos/{repo_id}/status", operation_id="getRepoStatus", response_model=models.RepoStatusResponse)
async def get_repo_status(request: Request, repo_id: str, principal: Principal = Depends(get_principal)):
    data = await request.app.state.services["github"].repo_status(principal, repo_id)
    return build_success(models.RepoStatusResponse, data)


@router.post("/github/issues", operation_id="createGitHubIssue", response_model=models.CreateIssueResponse, status_code=201)
async def create_github_issue(request: Request, body: models.CreateIssueRequest,
                              principal: Principal = Depends(require_mutating)):
    data = await request.app.state.services["github"].create_issue(
        principal, body.repository, body.title, body.body,
        _v(body.priority) if body.priority else None, body.labels)
    return build_success(models.CreateIssueResponse, data)

# Security registry (contract-drift test reads this at import time).
register("GET", "/integrations/github/repositories", "AUTH")
register("GET", "/github/repos/{repo_id}/status", "AUTH")
register("POST", "/github/issues", "AUTH")
