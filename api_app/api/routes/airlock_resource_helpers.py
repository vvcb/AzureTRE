from datetime import datetime
from collections import defaultdict
import logging
from typing import List

from fastapi import HTTPException
from starlette import status

from api.routes.resource_helpers import send_uninstall_message
from db.repositories.airlock_requests import AirlockRequestRepository
from db.repositories.resource_templates import ResourceTemplateRepository
from db.repositories.user_resources import UserResourceRepository
from db.repositories.workspace_services import WorkspaceServiceRepository
from db.repositories.operations import OperationRepository
from event_grid.event_sender import send_status_changed_event, send_airlock_notification_event
from models.domain.authentication import User
from models.domain.workspace import Workspace
from models.schemas.airlock_request import AirlockRequestWithAllowedUserActions
from models.domain.resource import ResourceType
from models.domain.airlock_request import AirlockActions, AirlockFile, AirlockRequest, AirlockRequestStatus, AirlockRequestType, AirlockReview, AirlockReviewUserResource
from models.domain.operation import Operation

from resources import strings
from services.authentication import get_access_service


async def save_and_publish_event_airlock_request(airlock_request: AirlockRequest, airlock_request_repo: AirlockRequestRepository, user: User, workspace: Workspace):

    # First check we have some email addresses so we can notify people.
    access_service = get_access_service()
    role_assignment_details = access_service.get_workspace_role_assignment_details(workspace)
    check_email_exists(role_assignment_details)

    try:
        logging.debug(f"Saving airlock request item: {airlock_request.id}")
        airlock_request.updatedBy = user
        airlock_request.updatedWhen = get_timestamp()
        airlock_request_repo.save_item(airlock_request)
    except Exception as e:
        logging.error(f'Failed saving airlock request {airlock_request}: {e}')
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=strings.STATE_STORE_ENDPOINT_NOT_RESPONDING)

    try:
        logging.debug(f"Sending status changed event for airlock request item: {airlock_request.id}")
        await send_status_changed_event(airlock_request=airlock_request, previous_status=None)
        await send_airlock_notification_event(airlock_request, role_assignment_details)
    except Exception as e:
        airlock_request_repo.delete_item(airlock_request.id)
        logging.error(f"Failed sending status_changed message: {e}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=strings.EVENT_GRID_GENERAL_ERROR_MESSAGE)


async def update_and_publish_event_airlock_request(
        airlock_request: AirlockRequest,
        airlock_request_repo: AirlockRequestRepository,
        updated_by: User,
        workspace: Workspace,
        new_status: AirlockRequestStatus = None,
        request_files: List[AirlockFile] = None,
        status_message: str = None,
        airlock_review: AirlockReview = None,
        review_user_resource: AirlockReviewUserResource = None) -> AirlockRequest:
    try:
        logging.debug(f"Updating airlock request item: {airlock_request.id}")
        updated_airlock_request = airlock_request_repo.update_airlock_request(
            original_request=airlock_request,
            updated_by=updated_by,
            new_status=new_status,
            request_files=request_files,
            status_message=status_message,
            airlock_review=airlock_review,
            review_user_resource=review_user_resource)
    except Exception as e:
        logging.error(f'Failed updating airlock_request item {airlock_request}: {e}')
        # If the validation failed, the error was not related to the saving itself
        if e.status_code == 400:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=strings.AIRLOCK_REQUEST_ILLEGAL_STATUS_CHANGE)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=strings.STATE_STORE_ENDPOINT_NOT_RESPONDING)

    if not new_status:
        logging.debug(f"Skipping sending 'status changed' event for airlock request item: {airlock_request.id} - there is no status change")
        return updated_airlock_request

    try:
        logging.debug(f"Sending status changed event for airlock request item: {airlock_request.id}")
        await send_status_changed_event(airlock_request=updated_airlock_request, previous_status=airlock_request.status)
        access_service = get_access_service()
        role_assignment_details = access_service.get_workspace_role_assignment_details(workspace)
        await send_airlock_notification_event(updated_airlock_request, role_assignment_details)
        return updated_airlock_request
    except Exception as e:
        logging.error(f"Failed sending status_changed message: {e}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=strings.EVENT_GRID_GENERAL_ERROR_MESSAGE)


def get_timestamp() -> float:
    return datetime.utcnow().timestamp()


def check_email_exists(role_assignment_details: defaultdict(list)):
    if "WorkspaceResearcher" not in role_assignment_details or not role_assignment_details["WorkspaceResearcher"]:
        logging.error('Creating an airlock request but the researcher does not have an email address.')
        raise HTTPException(status_code=status.HTTP_417_EXPECTATION_FAILED, detail=strings.AIRLOCK_NO_RESEARCHER_EMAIL)
    if "AirlockManager" not in role_assignment_details or not role_assignment_details["AirlockManager"]:
        logging.error('Creating an airlock request but the airlock manager does not have an email address.')
        raise HTTPException(status_code=status.HTTP_417_EXPECTATION_FAILED, detail=strings.AIRLOCK_NO_AIRLOCK_MANAGER_EMAIL)


def get_airlock_requests_by_user_and_workspace(user: User, workspace: Workspace, airlock_request_repo: AirlockRequestRepository,
                                               creator_user_id: str = None, type: AirlockRequestType = None, status: AirlockRequestStatus = None,
                                               order_by: str = None, order_ascending=True) -> List[AirlockRequest]:
    return airlock_request_repo.get_airlock_requests(workspace_id=workspace.id, creator_user_id=creator_user_id, type=type, status=status,
                                                     order_by=order_by, order_ascending=order_ascending)


def get_allowed_actions(request: AirlockRequest, user: User, airlock_request_repo: AirlockRequestRepository) -> AirlockRequestWithAllowedUserActions:
    allowed_actions = []

    can_review_request = airlock_request_repo.validate_status_update(request.status, AirlockRequestStatus.ApprovalInProgress)
    can_cancel_request = airlock_request_repo.validate_status_update(request.status, AirlockRequestStatus.Cancelled)
    can_submit_request = airlock_request_repo.validate_status_update(request.status, AirlockRequestStatus.Submitted)

    if can_review_request and "AirlockManager" in user.roles:
        allowed_actions.append(AirlockActions.Review)

    if can_cancel_request and ("WorkspaceOwner" in user.roles or "WorkspaceResearcher" in user.roles):
        allowed_actions.append(AirlockActions.Cancel)

    if can_submit_request and ("WorkspaceOwner" in user.roles or "WorkspaceResearcher" in user.roles):
        allowed_actions.append(AirlockActions.Submit)

    return allowed_actions


def enrich_requests_with_allowed_actions(requests: List[AirlockRequest], user: User, airlock_request_repo: AirlockRequestRepository) -> List[AirlockRequestWithAllowedUserActions]:
    enriched_requests = []
    for request in requests:
        allowed_actions = get_allowed_actions(request, user, airlock_request_repo)
        enriched_requests.append(AirlockRequestWithAllowedUserActions(airlockRequest=request, allowedUserActions=allowed_actions))
    return enriched_requests


async def delete_review_user_resources(
        airlock_request: AirlockRequest,
        user_resource_repo: UserResourceRepository,
        workspace_service_repo: WorkspaceServiceRepository,
        resource_template_repo: ResourceTemplateRepository,
        operations_repo: OperationRepository,
        user: User) -> List[Operation]:
    operations: List[Operation] = []
    for review_ur in airlock_request.reviewUserResources:
        user_resource = user_resource_repo.get_user_resource_by_id(
            workspace_id=review_ur.workspaceId,
            service_id=review_ur.workspaceServiceId,
            resource_id=review_ur.userResourceId
        )

        workspace_service = workspace_service_repo.get_workspace_service_by_id(workspace_id=user_resource.workspaceId, service_id=user_resource.parentWorkspaceServiceId)

        resource_template = resource_template_repo.get_template_by_name_and_version(
            user_resource.templateName,
            user_resource.templateVersion,
            ResourceType.UserResource,
            workspace_service.templateName)

        logging.info(f"Deleting user resource {user_resource.id} in workspace service {workspace_service.id}")
        operations.append(await send_uninstall_message(
            resource=user_resource,
            resource_repo=user_resource_repo,
            operations_repo=operations_repo,
            resource_type=ResourceType.UserResource,
            resource_template_repo=resource_template_repo,
            user=user,
            resource_template=resource_template))
        logging.info(f"Started operation {operations[-1]}")

    logging.info(f"Started {len(operations)} operations on deleting user resources")
    return operations
