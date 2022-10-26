import logging

from fastapi import APIRouter, Depends, HTTPException, status as status_code, Response

from jsonschema.exceptions import ValidationError

from db.repositories.user_resources import UserResourceRepository
from db.repositories.workspace_services import WorkspaceServiceRepository
from db.repositories.operations import OperationRepository
from db.repositories.resource_templates import ResourceTemplateRepository
from db.repositories.airlock_requests import AirlockRequestRepository
from db.errors import EntityDoesNotExist, UserNotAuthorizedToUseTemplate

from api.dependencies.database import get_repository
from api.dependencies.workspaces import get_workspace_by_id_from_path, get_deployed_workspace_by_id_from_path
from api.dependencies.airlock import get_airlock_request_by_id_from_path
from models.domain.airlock_request import AirlockRequest, AirlockRequestStatus, AirlockRequestType, AirlockReviewDecision, \
    AirlockReviewUserResource
from models.schemas.operation import OperationInResponse
from models.schemas.user_resource import UserResourceInCreate
from models.schemas.airlock_request_url import AirlockRequestTokenInResponse
from models.schemas.airlock_request import AirlockRequestAndOperationInResponse, AirlockRequestInCreate, AirlockRequestInResponse, \
    AirlockRequestWithAllowedUserActionsInList, AirlockReviewInCreate
from resources import strings
from services.authentication import get_current_workspace_owner_or_researcher_user_or_airlock_manager, \
    get_current_workspace_owner_or_researcher_user, get_current_airlock_manager_user

from .airlock_resource_helpers import save_and_publish_event_airlock_request, update_and_publish_event_airlock_request, \
    enrich_requests_with_allowed_actions, get_airlock_requests_by_user_and_workspace, delete_review_user_resources
from .resource_helpers import save_and_deploy_resource, construct_location_header

from services.airlock import validate_user_allowed_to_access_storage_account, \
    get_account_by_request, get_airlock_request_container_sas_token, validate_request_status

airlock_workspace_router = APIRouter(dependencies=[Depends(get_current_workspace_owner_or_researcher_user_or_airlock_manager)])


# airlock
@airlock_workspace_router.post("/workspaces/{workspace_id}/requests", status_code=status_code.HTTP_201_CREATED,
                               response_model=AirlockRequestInResponse, name=strings.API_CREATE_AIRLOCK_REQUEST,
                               dependencies=[Depends(get_current_workspace_owner_or_researcher_user), Depends(get_workspace_by_id_from_path)])
async def create_draft_request(airlock_request_input: AirlockRequestInCreate, user=Depends(get_current_workspace_owner_or_researcher_user),
                               airlock_request_repo=Depends(get_repository(AirlockRequestRepository)),
                               workspace=Depends(get_deployed_workspace_by_id_from_path)) -> AirlockRequestInResponse:
    if workspace.properties.get("enable_airlock") is False:
        raise HTTPException(status_code=status_code.HTTP_405_METHOD_NOT_ALLOWED, detail=strings.AIRLOCK_NOT_ENABLED_IN_WORKSPACE)
    try:
        airlock_request = airlock_request_repo.create_airlock_request_item(airlock_request_input, workspace.id, user)
    except (ValidationError, ValueError) as e:
        logging.error(f"Failed creating airlock request model instance: {e}")
        raise HTTPException(status_code=status_code.HTTP_400_BAD_REQUEST, detail=str(e))
    await save_and_publish_event_airlock_request(airlock_request, airlock_request_repo, user, workspace)
    return AirlockRequestInResponse(airlockRequest=airlock_request)


@airlock_workspace_router.get("/workspaces/{workspace_id}/requests",
                              status_code=status_code.HTTP_200_OK,
                              response_model=AirlockRequestWithAllowedUserActionsInList,
                              name=strings.API_LIST_AIRLOCK_REQUESTS,
                              dependencies=[Depends(get_current_workspace_owner_or_researcher_user_or_airlock_manager),
                                            Depends(get_workspace_by_id_from_path)])
async def get_all_airlock_requests_by_workspace(
        airlock_request_repo=Depends(get_repository(AirlockRequestRepository)),
        workspace=Depends(get_deployed_workspace_by_id_from_path),
        user=Depends(get_current_workspace_owner_or_researcher_user_or_airlock_manager),
        creator_user_id: str = None, type: AirlockRequestType = None, status: AirlockRequestStatus = None,
        order_by: str = None, order_ascending: bool = True) -> AirlockRequestWithAllowedUserActionsInList:
    try:
        airlock_requests = get_airlock_requests_by_user_and_workspace(user=user, workspace=workspace, airlock_request_repo=airlock_request_repo,
                                                                      creator_user_id=creator_user_id, type=type, status=status,
                                                                      order_by=order_by, order_ascending=order_ascending)
        airlock_requests_with_allowed_user_actions = enrich_requests_with_allowed_actions(airlock_requests, user, airlock_request_repo)
    except (ValidationError, ValueError) as e:
        logging.error(f"Failed retrieving all the airlock requests for a workspace: {e}")
        raise HTTPException(status_code=status_code.HTTP_400_BAD_REQUEST, detail=str(e))
    return AirlockRequestWithAllowedUserActionsInList(airlockRequests=airlock_requests_with_allowed_user_actions)


@airlock_workspace_router.get("/workspaces/{workspace_id}/requests/{airlock_request_id}", status_code=status_code.HTTP_200_OK,
                              response_model=AirlockRequestInResponse, name=strings.API_GET_AIRLOCK_REQUEST,
                              dependencies=[Depends(get_current_workspace_owner_or_researcher_user), Depends(get_workspace_by_id_from_path)])
async def retrieve_airlock_request_by_id(airlock_request=Depends(get_airlock_request_by_id_from_path)) -> AirlockRequestInResponse:
    return AirlockRequestInResponse(airlockRequest=airlock_request)


@airlock_workspace_router.post("/workspaces/{workspace_id}/requests/{airlock_request_id}/submit", status_code=status_code.HTTP_200_OK,
                               response_model=AirlockRequestInResponse, name=strings.API_SUBMIT_AIRLOCK_REQUEST,
                               dependencies=[Depends(get_current_workspace_owner_or_researcher_user), Depends(get_workspace_by_id_from_path)])
async def create_submit_request(airlock_request=Depends(get_airlock_request_by_id_from_path),
                                user=Depends(get_current_workspace_owner_or_researcher_user),
                                airlock_request_repo=Depends(get_repository(AirlockRequestRepository)),
                                workspace=Depends(get_workspace_by_id_from_path)) -> AirlockRequestInResponse:
    updated_resource = await update_and_publish_event_airlock_request(airlock_request, airlock_request_repo, user, workspace,
                                                                      new_status=AirlockRequestStatus.Submitted)
    return AirlockRequestInResponse(airlockRequest=updated_resource)


@airlock_workspace_router.post("/workspaces/{workspace_id}/requests/{airlock_request_id}/cancel", status_code=status_code.HTTP_200_OK,
                               response_model=AirlockRequestInResponse, name=strings.API_CANCEL_AIRLOCK_REQUEST,
                               dependencies=[Depends(get_current_workspace_owner_or_researcher_user), Depends(get_workspace_by_id_from_path)])
async def create_cancel_request(airlock_request=Depends(get_airlock_request_by_id_from_path),
                                user=Depends(get_current_workspace_owner_or_researcher_user),
                                airlock_request_repo=Depends(get_repository(AirlockRequestRepository)),
                                workspace=Depends(get_workspace_by_id_from_path)) -> AirlockRequestInResponse:
    updated_resource = await update_and_publish_event_airlock_request(airlock_request, airlock_request_repo, user, workspace,
                                                                      new_status=AirlockRequestStatus.Cancelled)
    return AirlockRequestInResponse(airlockRequest=updated_resource)


@airlock_workspace_router.post("/workspaces/{workspace_id}/requests/{airlock_request_id}/review-user-resource",
                               status_code=status_code.HTTP_202_ACCEPTED, response_model=AirlockRequestAndOperationInResponse,
                               name=strings.API_CREATE_AIRLOCK_REVIEW_USER_RESOURCE,
                               dependencies=[Depends(get_current_airlock_manager_user), Depends(get_workspace_by_id_from_path)])
async def create_review_user_resource(
        response: Response,
        airlock_request=Depends(get_airlock_request_by_id_from_path),
        user=Depends(get_current_airlock_manager_user),
        workspace=Depends(get_deployed_workspace_by_id_from_path),
        user_resource_repo=Depends(get_repository(UserResourceRepository)),
        workspace_service_repo=Depends(get_repository(WorkspaceServiceRepository)),
        operation_repo=Depends(get_repository(OperationRepository)),
        airlock_request_repo=Depends(get_repository(AirlockRequestRepository)),
        resource_template_repo=Depends(get_repository(ResourceTemplateRepository))) -> OperationInResponse:

    if airlock_request.status != AirlockRequestStatus.InReview:
        raise HTTPException(status_code=status_code.HTTP_400_BAD_REQUEST,
                            detail="Airlock request must be in 'in_review' status to create a Review User Resource")

    try:
        # Getting the review configuration from the airlock request's workspace properties
        if airlock_request.type == AirlockRequestType.Import:
            config = workspace.properties["airlock_review_config"]["import"]
            workspace_id = config["workspace_id"]
        else:
            assert airlock_request.type == AirlockRequestType.Export
            config = workspace.properties["airlock_review_config"]["export"]
            workspace_id = workspace.id
        workspace_service_id = config["workspace_service_id"]
        user_resource_template_name = config["user_resource_template_name"]

        logging.info(f"Going to create a user resource in {workspace_id} {workspace_service_id} {user_resource_template_name}")
    except (KeyError, TypeError) as e:
        logging.error(f"Failed to parse configuration: {e}")
        raise HTTPException(status_code=status_code.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"Failed to retrieve Airlock Review configuration for workspace {workspace.id}.\
                            Please ask your TRE administrator to check the configuration. Details: {str(e)}")

    # Find workspace service to create user resource in
    try:
        workspace_service = workspace_service_repo.get_workspace_service_by_id(workspace_id=workspace_id, service_id=workspace_service_id)
    except EntityDoesNotExist as e:
        logging.error(f"Failed to get workspace service {workspace_service_id} for workspace {workspace_id}: {str(e)}")
        raise HTTPException(status_code=status_code.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"Failed to retrieve Airlock Review configuration for workspace {workspace.id}.\
                            Please ask your TRE administrator to check the configuration. Details: {str(e)}")

    # Getting the SAS URL (this function raises HTTPException in case of error)
    airlock_request_sas_url = get_airlock_container_link(airlock_request, user, workspace)

    # Now have all components for user resource, create an object for it
    user_resource_create = UserResourceInCreate(
        templateName=user_resource_template_name,
        properties={
            "display_name": "Airlock Review VM",
            "description": f"Airlock Review VM for request {airlock_request.title} (ID {airlock_request.id})",
            "airlock_request_sas_url": airlock_request_sas_url
        }
    )

    # Start VM creation
    try:
        user_resource, resource_template = user_resource_repo.create_user_resource_item(
            user_resource_create, workspace_id, workspace_service_id, workspace_service.templateName, user.id, user.roles)
    except (ValidationError, ValueError) as e:
        logging.error(f"Failed create user resource model instance due to validation error: {e}")
        raise HTTPException(status_code=status_code.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f"Invalid configuration for creating user resource. Please contact your TRE administrator. \
                            Details: {str(e)}")
    except UserNotAuthorizedToUseTemplate as e:
        logging.error(f"User not authorized to use template: {e}")
        raise HTTPException(status_code=status_code.HTTP_403_FORBIDDEN, detail=str(e))

    operation = await save_and_deploy_resource(
        resource=user_resource,
        resource_repo=user_resource_repo,
        operations_repo=operation_repo,
        resource_template_repo=resource_template_repo,
        user=user,
        resource_template=resource_template)

    # Update the Airlock Request with the information on the VM
    updated_resource = await update_and_publish_event_airlock_request(
        airlock_request,
        airlock_request_repo,
        user,
        workspace,
        review_user_resource=AirlockReviewUserResource(
            workspaceId=workspace_id,
            workspaceServiceId=workspace_service_id,
            userResourceId=user_resource.id
        ))
    logging.info(f"Airlock Request {updated_resource.id} updated to include {updated_resource.reviewUserResources}")

    response.headers["Location"] = construct_location_header(operation)
    return AirlockRequestAndOperationInResponse(airlockRequest=updated_resource, operation=operation)


@airlock_workspace_router.post("/workspaces/{workspace_id}/requests/{airlock_request_id}/review",
                               status_code=status_code.HTTP_200_OK, response_model=AirlockRequestInResponse,
                               name=strings.API_REVIEW_AIRLOCK_REQUEST, dependencies=[Depends(get_current_airlock_manager_user),
                                                                                      Depends(get_workspace_by_id_from_path)])
async def create_airlock_review(
        airlock_review_input: AirlockReviewInCreate,
        airlock_request=Depends(get_airlock_request_by_id_from_path),
        user=Depends(get_current_airlock_manager_user),
        workspace=Depends(get_deployed_workspace_by_id_from_path),
        airlock_request_repo=Depends(get_repository(AirlockRequestRepository)),
        user_resource_repo=Depends(get_repository(UserResourceRepository)),
        workspace_service_repo=Depends(get_repository(WorkspaceServiceRepository)),
        operation_repo=Depends(get_repository(OperationRepository)),
        resource_template_repo=Depends(get_repository(ResourceTemplateRepository))) -> AirlockRequestInResponse:

    try:
        airlock_review = airlock_request_repo.create_airlock_review_item(airlock_review_input, user)
    except (ValidationError, ValueError) as e:
        logging.error(f"Failed creating airlock review model instance: {e}")
        raise HTTPException(status_code=status_code.HTTP_400_BAD_REQUEST, detail=str(e))
    # Store review with new status in cosmos, and send status_changed event
    if airlock_review.reviewDecision.value == AirlockReviewDecision.Approved:
        review_status = AirlockRequestStatus.ApprovalInProgress
    elif airlock_review.reviewDecision.value == AirlockReviewDecision.Rejected:
        review_status = AirlockRequestStatus.RejectionInProgress

    updated_airlock_request = await update_and_publish_event_airlock_request(airlock_request=airlock_request,
                                                                             airlock_request_repo=airlock_request_repo, updated_by=user,
                                                                             workspace=workspace, new_status=review_status,
                                                                             airlock_review=airlock_review)

    # If there was a VM created for the request, clean it up as it will no longer be needed
    # In this request, we aren't returning the operations for clean up of VMs,
    # however the operations still will be saved in the DB and displayed on the UI as normal.
    _ = await delete_review_user_resources(
        airlock_request=airlock_request,
        user_resource_repo=user_resource_repo,
        workspace_service_repo=workspace_service_repo,
        resource_template_repo=resource_template_repo,
        operations_repo=operation_repo,
        user=user
    )

    return AirlockRequestInResponse(airlockRequest=updated_airlock_request)


def get_airlock_container_link(airlock_request: AirlockRequest, user, workspace):
    validate_user_allowed_to_access_storage_account(user, airlock_request)
    validate_request_status(airlock_request)
    account_name: str = get_account_by_request(airlock_request, workspace)
    return get_airlock_request_container_sas_token(account_name, airlock_request)


@airlock_workspace_router.get("/workspaces/{workspace_id}/requests/{airlock_request_id}/link",
                              status_code=status_code.HTTP_200_OK, response_model=AirlockRequestTokenInResponse,
                              name=strings.API_AIRLOCK_REQUEST_LINK,
                              dependencies=[Depends(get_current_workspace_owner_or_researcher_user_or_airlock_manager)])
async def get_airlock_container_link_method(workspace=Depends(get_deployed_workspace_by_id_from_path),
                                            airlock_request=Depends(get_airlock_request_by_id_from_path),
                                            user=Depends(get_current_workspace_owner_or_researcher_user_or_airlock_manager)) -> AirlockRequestTokenInResponse:
    container_url = get_airlock_container_link(airlock_request, user, workspace)
    return AirlockRequestTokenInResponse(containerUrl=container_url)
