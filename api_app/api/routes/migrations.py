import logging

from fastapi import APIRouter, Depends, HTTPException, status
from db.migrations.airlock import AirlockMigration
from db.migrations.resources import ResourceMigration
from db.repositories.operations import OperationRepository
from services.authentication import get_current_admin_user
from resources import strings
from api.dependencies.database import get_repository
from db.migrations.shared_services import SharedServiceMigration
from db.migrations.workspaces import WorkspaceMigration
from db.repositories.resources import ResourceRepository
from models.schemas.migrations import MigrationOutList, Migration


migrations_core_router = APIRouter(dependencies=[Depends(get_current_admin_user)])


@migrations_core_router.post("/migrations",
                             status_code=status.HTTP_202_ACCEPTED,
                             name=strings.API_MIGRATE_DATABASE,
                             response_model=MigrationOutList,
                             dependencies=[Depends(get_current_admin_user)])
async def migrate_database(resources_repo=Depends(get_repository(ResourceRepository)),
                           operations_repo=Depends(get_repository(OperationRepository)),
                           shared_services_migration=Depends(get_repository(SharedServiceMigration)),
                           workspace_migration=Depends(get_repository(WorkspaceMigration)),
                           resource_migration=Depends(get_repository(ResourceMigration)),
                           airlock_migration=Depends(get_repository(AirlockMigration))):
    try:
        migrations = list()
        logging.info("PR 1030")
        resources_repo.rename_field_name('resourceTemplateName', 'templateName')
        resources_repo.rename_field_name('resourceTemplateVersion', 'templateVersion')
        resources_repo.rename_field_name('resourceTemplateParameters', 'properties')
        migrations.append(Migration(issueNumber="PR 1030", status="Executed"))

        logging.info("PR 1031")
        resources_repo.rename_field_name('workspaceType', 'templateName')
        resources_repo.rename_field_name('workspaceServiceType', 'templateName')
        resources_repo.rename_field_name('userResourceType', 'templateName')
        migrations.append(Migration(issueNumber="PR 1031", status="Executed"))

        logging.info("PR 1717 - Shared services")
        migration_status = "Executed" if shared_services_migration.deleteDuplicatedSharedServices() else "Skipped"
        migrations.append(Migration(issueNumber="PR 1717", status=migration_status))

        logging.info("PR 1726 - Authentication needs to be in properties so we can update them")
        migration_status = "Executed" if workspace_migration.moveAuthInformationToProperties() else "Skipped"
        migrations.append(Migration(issueNumber="PR 1726", status=migration_status))

        logging.info("PR 1406 - Extra field to support UI")
        num_rows = resource_migration.add_deployment_status_field(operations_repo)
        migrations.append(Migration(issueNumber="1406", status=f'Updated {num_rows} resource objects'))

        logging.info("PR 2371 - Validate min firewall version")
        shared_services_migration.checkMinFirewallVersion()
        migrations.append(Migration(issueNumber="2371", status='Firewall version meets requirement'))

        logging.info("PR 2779 - Restructure Airlock requests & add createdBy field")
        airlock_migration.rename_field_name('requestType', 'type')
        airlock_migration.rename_field_name('requestTitle', 'title')
        airlock_migration.rename_field_name('user', 'updatedBy')
        airlock_migration.rename_field_name('creationTime', 'createdWhen')
        num_updated = airlock_migration.add_created_by_and_rename_in_history()
        migrations.append(Migration(issueNumber="2779", status=f'Renamed fields & updated {num_updated} airlock requests with createdBy'))

        return MigrationOutList(migrations=migrations)
    except Exception as e:
        logging.error("Failed to migrate database: %s", e, exc_info=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
