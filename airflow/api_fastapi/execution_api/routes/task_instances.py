# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import Body, HTTPException, status
from pydantic import JsonValue
from sqlalchemy import update
from sqlalchemy.exc import NoResultFound, SQLAlchemyError
from sqlalchemy.sql import select

from airflow.api_fastapi.common.db.common import SessionDep
from airflow.api_fastapi.common.router import AirflowRouter
from airflow.api_fastapi.execution_api.datamodels.taskinstance import (
    DagRun,
    TIDeferredStatePayload,
    TIEnterRunningPayload,
    TIHeartbeatInfo,
    TIRescheduleStatePayload,
    TIRunContext,
    TIStateUpdate,
    TITerminalStatePayload,
)
from airflow.models.dagrun import DagRun as DR
from airflow.models.taskinstance import TaskInstance as TI, _update_rtif
from airflow.models.taskreschedule import TaskReschedule
from airflow.models.trigger import Trigger
from airflow.utils import timezone
from airflow.utils.state import State

# TODO: Add dependency on JWT token
router = AirflowRouter()


log = logging.getLogger(__name__)


@router.patch(
    "/{task_instance_id}/run",
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Task Instance not found"},
        status.HTTP_409_CONFLICT: {"description": "The TI is already in the requested state"},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"description": "Invalid payload for the state transition"},
    },
)
def ti_run(
    task_instance_id: UUID, ti_run_payload: Annotated[TIEnterRunningPayload, Body()], session: SessionDep
) -> TIRunContext:
    """
    Run a TaskInstance.

    This endpoint is used to start a TaskInstance that is in the QUEUED state.
    """
    # We only use UUID above for validation purposes
    ti_id_str = str(task_instance_id)

    old = select(TI.state, TI.dag_id, TI.run_id).where(TI.id == ti_id_str).with_for_update()
    try:
        (previous_state, dag_id, run_id) = session.execute(old).one()
    except NoResultFound:
        log.error("Task Instance %s not found", ti_id_str)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "not_found",
                "message": "Task Instance not found",
            },
        )

    # We exclude_unset to avoid updating fields that are not set in the payload
    data = ti_run_payload.model_dump(exclude_unset=True)

    query = update(TI).where(TI.id == ti_id_str).values(data)

    # TODO: We will need to change this for other states like:
    #   reschedule, retry, defer etc.
    if previous_state != State.QUEUED:
        log.warning(
            "Can not start Task Instance ('%s') in invalid state: %s",
            ti_id_str,
            previous_state,
        )

        # TODO: Pass a RFC 9457 compliant error message in "detail" field
        # https://datatracker.ietf.org/doc/html/rfc9457
        # to provide more information about the error
        # FastAPI will automatically convert this to a JSON response
        # This might be added in FastAPI in https://github.com/fastapi/fastapi/issues/10370
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "invalid_state",
                "message": "TI was not in a state where it could be marked as running",
                "previous_state": previous_state,
            },
        )
    log.info("Task with %s state started on %s ", previous_state, ti_run_payload.hostname)
    # Ensure there is no end date set.
    query = query.values(
        end_date=None,
        hostname=ti_run_payload.hostname,
        unixname=ti_run_payload.unixname,
        pid=ti_run_payload.pid,
        state=State.RUNNING,
    )

    try:
        result = session.execute(query)
        log.info("TI %s state updated: %s row(s) affected", ti_id_str, result.rowcount)

        dr = session.execute(
            select(
                DR.run_id,
                DR.dag_id,
                DR.data_interval_start,
                DR.data_interval_end,
                DR.start_date,
                DR.end_date,
                DR.run_type,
                DR.conf,
                DR.logical_date,
            ).filter_by(dag_id=dag_id, run_id=run_id)
        ).one_or_none()

        if not dr:
            raise ValueError(f"DagRun with dag_id={dag_id} and run_id={run_id} not found.")

        return TIRunContext(
            dag_run=DagRun.model_validate(dr, from_attributes=True),
            # TODO: Add variables and connections that are needed (and has perms) for the task
            variables=[],
            connections=[],
        )
    except SQLAlchemyError as e:
        log.error("Error marking Task Instance state as running: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Database error occurred"
        )


@router.patch(
    "/{task_instance_id}/state",
    status_code=status.HTTP_204_NO_CONTENT,
    # TODO: Add description to the operation
    # TODO: Add Operation ID to control the function name in the OpenAPI spec
    # TODO: Do we need to use create_openapi_http_exception_doc here?
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Task Instance not found"},
        status.HTTP_409_CONFLICT: {"description": "The TI is already in the requested state"},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"description": "Invalid payload for the state transition"},
    },
)
def ti_update_state(
    task_instance_id: UUID,
    ti_patch_payload: Annotated[TIStateUpdate, Body()],
    session: SessionDep,
):
    """
    Update the state of a TaskInstance.

    Not all state transitions are valid, and transitioning to some states requires extra information to be
    passed along. (Check out the datamodels for details, the rendered docs might not reflect this accurately)
    """
    # We only use UUID above for validation purposes
    ti_id_str = str(task_instance_id)

    old = select(TI.state).where(TI.id == ti_id_str).with_for_update()
    try:
        (previous_state,) = session.execute(old).one()
    except NoResultFound:
        log.error("Task Instance %s not found", ti_id_str)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "not_found",
                "message": "Task Instance not found",
            },
        )

    # We exclude_unset to avoid updating fields that are not set in the payload
    data = ti_patch_payload.model_dump(exclude_unset=True)

    query = update(TI).where(TI.id == ti_id_str).values(data)

    if isinstance(ti_patch_payload, TITerminalStatePayload):
        query = TI.duration_expression_update(ti_patch_payload.end_date, query, session.bind)
        query = query.values(state=ti_patch_payload.state)
        if ti_patch_payload.state == State.FAILED:
            # clear the next_method and next_kwargs
            query = query.values(next_method=None, next_kwargs=None)
    elif isinstance(ti_patch_payload, TIDeferredStatePayload):
        # Calculate timeout if it was passed
        timeout = None
        if ti_patch_payload.trigger_timeout is not None:
            timeout = timezone.utcnow() + ti_patch_payload.trigger_timeout

        trigger_row = Trigger(
            classpath=ti_patch_payload.classpath,
            kwargs=ti_patch_payload.trigger_kwargs,
        )
        session.add(trigger_row)

        # TODO: HANDLE execution timeout later as it requires a call to the DB
        # either get it from the serialised DAG or get it from the API

        query = update(TI).where(TI.id == ti_id_str)
        query = query.values(
            state=State.DEFERRED,
            trigger_id=trigger_row.id,
            next_method=ti_patch_payload.next_method,
            next_kwargs=ti_patch_payload.trigger_kwargs,
            trigger_timeout=timeout,
        )
    elif isinstance(ti_patch_payload, TIRescheduleStatePayload):
        task_instance = session.get(TI, ti_id_str)
        actual_start_date = timezone.utcnow()
        session.add(
            TaskReschedule(
                task_instance.task_id,
                task_instance.dag_id,
                task_instance.run_id,
                task_instance.try_number,
                actual_start_date,
                ti_patch_payload.end_date,
                ti_patch_payload.reschedule_date,
                task_instance.map_index,
            )
        )

        query = update(TI).where(TI.id == ti_id_str)
        # calculate the duration for TI table too
        query = TI.duration_expression_update(ti_patch_payload.end_date, query, session.bind)
        # clear the next_method and next_kwargs so that none of the retries pick them up
        query = query.values(state=State.UP_FOR_RESCHEDULE, next_method=None, next_kwargs=None)
    # TODO: Replace this with FastAPI's Custom Exception handling:
    # https://fastapi.tiangolo.com/tutorial/handling-errors/#install-custom-exception-handlers
    try:
        result = session.execute(query)
        log.info("TI %s state updated: %s row(s) affected", ti_id_str, result.rowcount)
    except SQLAlchemyError as e:
        log.error("Error updating Task Instance state: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Database error occurred"
        )


@router.put(
    "/{task_instance_id}/heartbeat",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Task Instance not found"},
        status.HTTP_409_CONFLICT: {
            "description": "The TI attempting to heartbeat should be terminated for the given reason"
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"description": "Invalid payload for the state transition"},
    },
)
def ti_heartbeat(
    task_instance_id: UUID,
    ti_payload: TIHeartbeatInfo,
    session: SessionDep,
):
    """Update the heartbeat of a TaskInstance to mark it as alive & still running."""
    ti_id_str = str(task_instance_id)

    # Hot path: since heartbeating a task is a very common operation, we try to do minimize the number of queries
    # and DB round trips as much as possible.

    old = select(TI.state, TI.hostname, TI.pid).where(TI.id == ti_id_str).with_for_update()

    try:
        (previous_state, hostname, pid) = session.execute(old).one()
    except NoResultFound:
        log.error("Task Instance %s not found", ti_id_str)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "not_found",
                "message": "Task Instance not found",
            },
        )

    if hostname != ti_payload.hostname or pid != ti_payload.pid:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "running_elsewhere",
                "message": "TI is already running elsewhere",
                "current_hostname": hostname,
                "current_pid": pid,
            },
        )

    if previous_state != State.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "not_running",
                "message": "TI is no longer in the running state and task should terminate",
                "current_state": previous_state,
            },
        )

    # Update the last heartbeat time!
    session.execute(update(TI).where(TI.id == ti_id_str).values(last_heartbeat_at=timezone.utcnow()))
    log.debug("Task with %s state heartbeated", previous_state)


@router.put(
    "/{task_instance_id}/rtif",
    status_code=status.HTTP_201_CREATED,
    # TODO: Add description to the operation
    # TODO: Add Operation ID to control the function name in the OpenAPI spec
    # TODO: Do we need to use create_openapi_http_exception_doc here?
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Task Instance not found"},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "Invalid payload for the setting rendered task instance fields"
        },
    },
)
def ti_put_rtif(
    task_instance_id: UUID,
    put_rtif_payload: Annotated[dict[str, JsonValue], Body()],
    session: SessionDep,
):
    """Add an RTIF entry for a task instance, sent by the worker."""
    ti_id_str = str(task_instance_id)
    task_instance = session.scalar(select(TI).where(TI.id == ti_id_str))
    if not task_instance:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
        )
    _update_rtif(task_instance, put_rtif_payload, session)

    return {"message": "Rendered task instance fields successfully set"}
