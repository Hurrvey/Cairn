"""Administrator HTTP endpoints for model/provider configuration."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status

from cairn.authz.deps import require_permission
from cairn.authz.model import Principal
from cairn.core.ids import decode_id
from cairn.modelgw.schemas import (
    CreateModelRequest,
    CreateProviderRequest,
    ModelResponse,
    ModelTestResponse,
    ProviderCredentialsRequest,
    ProviderResponse,
)
from cairn.modelgw.service import ModelManagementService

__all__ = ["router"]

router = APIRouter(prefix="/v1", tags=["models"])

PROBLEM: dict[int | str, dict[str, Any]] = {
    403: {"content": {"application/problem+json": {}}},
    404: {"content": {"application/problem+json": {}}},
    409: {"content": {"application/problem+json": {}}},
}


def _service(request: Request) -> ModelManagementService:
    service: ModelManagementService = request.app.state.model_management_service
    return service


@router.post(
    "/model-providers",
    response_model=ProviderResponse,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEM,
)
async def create_provider(
    actor: Annotated[Principal, Depends(require_permission("platform:models"))],
    body: CreateProviderRequest,
    service: Annotated[ModelManagementService, Depends(_service)],
) -> ProviderResponse:
    return ProviderResponse.from_dto(await service.create_provider(actor.workspace_id, body))


@router.get("/model-providers", response_model=list[ProviderResponse], responses=PROBLEM)
async def list_providers(
    actor: Annotated[Principal, Depends(require_permission("platform:models"))],
    service: Annotated[ModelManagementService, Depends(_service)],
) -> list[ProviderResponse]:
    return [
        ProviderResponse.from_dto(item) for item in await service.list_providers(actor.workspace_id)
    ]


@router.delete(
    "/model-providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT, responses=PROBLEM
)
async def delete_provider(
    provider_id: str,
    actor: Annotated[Principal, Depends(require_permission("platform:models"))],
    service: Annotated[ModelManagementService, Depends(_service)],
) -> Response:
    await service.delete_provider(actor.workspace_id, decode_id("prov", provider_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/model-providers/{provider_id}/credentials",
    response_model=ProviderResponse,
    responses=PROBLEM,
    summary="Replace a provider's API key",
    description="Write-only: the key is stored encrypted and never returned.",
)
async def replace_provider_credentials(
    provider_id: str,
    actor: Annotated[Principal, Depends(require_permission("platform:models"))],
    body: ProviderCredentialsRequest,
    service: Annotated[ModelManagementService, Depends(_service)],
) -> ProviderResponse:
    return ProviderResponse.from_dto(
        await service.replace_credentials(actor.workspace_id, decode_id("prov", provider_id), body)
    )


@router.post(
    "/models",
    response_model=ModelResponse,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEM,
)
async def create_model(
    actor: Annotated[Principal, Depends(require_permission("platform:models"))],
    body: CreateModelRequest,
    service: Annotated[ModelManagementService, Depends(_service)],
) -> ModelResponse:
    return ModelResponse.from_dto(await service.create_model(actor.workspace_id, body))


@router.get("/models", response_model=list[ModelResponse], responses=PROBLEM)
async def list_models(
    actor: Annotated[Principal, Depends(require_permission("platform:models"))],
    service: Annotated[ModelManagementService, Depends(_service)],
) -> list[ModelResponse]:
    return [ModelResponse.from_dto(item) for item in await service.list_models(actor.workspace_id)]


@router.delete("/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT, responses=PROBLEM)
async def delete_model(
    model_id: str,
    actor: Annotated[Principal, Depends(require_permission("platform:models"))],
    service: Annotated[ModelManagementService, Depends(_service)],
) -> Response:
    await service.delete_model(actor.workspace_id, decode_id("mdl", model_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/models/{model_id}/test", response_model=ModelTestResponse, responses=PROBLEM)
async def test_model(
    model_id: str,
    actor: Annotated[Principal, Depends(require_permission("platform:models"))],
    service: Annotated[ModelManagementService, Depends(_service)],
) -> ModelTestResponse:
    result = await service.test_model(actor.workspace_id, decode_id("mdl", model_id))
    return ModelTestResponse(
        healthy=result.healthy,
        dimensions=result.dimensions,
        tokens=result.tokens,
        sparse_terms=result.sparse_terms,
    )
