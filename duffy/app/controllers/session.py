"""This is the session controller."""
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.status import (
    HTTP_201_CREATED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_422_UNPROCESSABLE_ENTITY,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from ...api_models import (
    SessionCreateModel,
    SessionResult,
    SessionResultCollection,
    SessionUpdateModel,
)
from ...database.model import Node, Session, SessionNode, Tenant
from ...database.types import NodeState
from ...nodes_context import contextualize, decontextualize
from ...tasks import deprovision_nodes, fill_pools
from ..auth import req_tenant, req_tenant_optional
from ..database import req_db_async_session

log = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions")


# http get http://localhost:8080/api/v1/sessions
@router.get("", response_model=SessionResultCollection, tags=["sessions"])
async def get_all_sessions(
    db_async_session: AsyncSession = Depends(req_db_async_session),
    tenant: Optional[Tenant] = Depends(req_tenant_optional),
):
    """Return all sessions."""
    query = (
        select(Session)
        .options(
            selectinload(Session.tenant),
            selectinload(Session.session_nodes).selectinload(SessionNode.node),
        )
        .filter_by(active=True)
    )
    if tenant and not tenant.is_admin:
        query = query.filter_by(tenant=tenant)
    results = await db_async_session.execute(query)
    return {"action": "get", "sessions": results.scalars().all()}


# http get http://localhost:8080/api/v1/sessions/2
@router.get("/{id}", response_model=SessionResult, tags=["sessions"])
async def get_session(
    id: int,
    db_async_session: AsyncSession = Depends(req_db_async_session),
    tenant: Tenant = Depends(req_tenant),
):
    """Return a session with the specified **ID**."""
    session = (
        await db_async_session.execute(
            select(Session)
            .filter_by(id=id)
            .options(
                selectinload(Session.tenant),
                selectinload(Session.session_nodes).selectinload(SessionNode.node),
            )
        )
    ).scalar_one_or_none()
    if not session:
        raise HTTPException(HTTP_404_NOT_FOUND)
    if not tenant.is_admin and session.tenant != tenant:
        raise HTTPException(HTTP_403_FORBIDDEN)
    return {"action": "get", "session": session}


# http --json post http://localhost:8080/api/v1/sessions tenant_id=2 \
#     'nodes_specs:=[{"pool": "virtual-fedora34-x86_64-small", "quantity": 1}]
@router.post("", status_code=HTTP_201_CREATED, response_model=SessionResult, tags=["sessions"])
async def create_session(
    data: SessionCreateModel,
    response: Response,
    db_async_session: AsyncSession = Depends(req_db_async_session),
    tenant: Tenant = Depends(req_tenant),
):
    """Create a session with the requested nodes specs."""
    if tenant.is_admin and data.tenant_id is not None:
        tenant = (
            await db_async_session.execute(select(Tenant).filter_by(id=data.tenant_id))
        ).scalar_one_or_none()

        if not tenant:
            raise HTTPException(
                HTTP_422_UNPROCESSABLE_ENTITY, f"can't find tenant with id {data.tenant_id}"
            )
        elif not tenant.active:
            raise HTTPException(
                HTTP_422_UNPROCESSABLE_ENTITY, f"tenant '{tenant.name}' isn't active"
            )
    elif not tenant.is_admin and data.tenant_id is not None and data.tenant_id != tenant.id:
        raise HTTPException(HTTP_403_FORBIDDEN, "can't create session for other tenant")

    session = Session(
        tenant=tenant, data={"nodes_specs": [spec.dict() for spec in data.nodes_specs]}
    )
    db_async_session.add(session)

    nodes_in_transaction = []
    pools_to_fill_up = set()
    for nodes_spec in data.nodes_specs:
        pools_to_fill_up.add(nodes_spec.pool)
        nodes_spec_dict = nodes_spec.dict()
        quantity = nodes_spec_dict.pop("quantity")

        query = (
            select(Node)
            .filter_by(active=True, state=NodeState.ready, **nodes_spec_dict)
            .limit(quantity)
        )

        nodes_to_reserve = (await db_async_session.execute(query)).scalars().all()
        nodes_in_transaction.extend(nodes_to_reserve)

        if len(nodes_to_reserve) < quantity:
            raise HTTPException(HTTP_422_UNPROCESSABLE_ENTITY, f"can't reserve nodes: {nodes_spec}")

        # take the nodes out of circulation and update data
        for node in nodes_to_reserve:
            # record why this node was allocated for this session
            node.data["nodes_spec"] = nodes_spec.dict()
            node.state = NodeState.contextualizing
            session_node = SessionNode(
                session=session, node=node, pool=nodes_spec.pool, data=node.data
            )
            db_async_session.add(session_node)

    await db_async_session.flush()
    nodes_in_transaction = await asyncio.gather(
        *(db_async_session.merge(node, load=False) for node in nodes_in_transaction)
    )
    # Meh. Reload the session instance to ensure all related objects are present in the session.
    session = (
        await db_async_session.execute(
            select(Session)
            .filter_by(id=session.id)
            .options(
                selectinload(Session.tenant),
                selectinload(Session.session_nodes).selectinload(SessionNode.node),
            )
        )
    ).scalar_one()

    contextualized_ipaddrs = await contextualize(
        nodes=[node.ipaddr for node in nodes_in_transaction], ssh_pubkey=tenant.ssh_key
    )

    if None in contextualized_ipaddrs:
        log.error("One or more nodes couldn't be contextualized:")
        nodes_to_decontextualize = []
        for node, ipaddr in zip(nodes_in_transaction, contextualized_ipaddrs):
            if not ipaddr:
                log.error("    id: %s hostname: %s ipaddr: %s", node.id, node.hostname, node.ipaddr)
                node.state = NodeState.failed
                node.data["error"] = "contextualizing node failed"
            else:
                nodes_to_decontextualize.append(node.ipaddr)

        decontextualized_ipaddrs = await decontextualize(nodes=nodes_to_decontextualize)

        if None in decontextualized_ipaddrs:
            log.error("One or more nodes couldn't be decontextualized:")
            for node, ipaddr in zip(nodes_in_transaction, decontextualized_ipaddrs):
                if not ipaddr:
                    log.error(
                        "    id: %s hostname: %s ipaddr: %s", node.id, node.hostname, node.ipaddr
                    )
                    node.state = NodeState.failed
                    node.data["error"] = "decontextualizing node failed"

        await db_async_session.commit()
        response.headers["Retry-After"] = "0"
        raise HTTPException(HTTP_503_SERVICE_UNAVAILABLE, "contextualization of nodes failed")
    else:  # None not in contextualized_ipaddrs
        for node in nodes_in_transaction:
            node.state = NodeState.deployed

    try:
        await db_async_session.commit()
    except IntegrityError as exc:  # pragma: no cover
        raise HTTPException(HTTP_422_UNPROCESSABLE_ENTITY, str(exc))

    # Tell backend worker to fill up pools from which nodes were taken.
    fill_pools.delay(pool_names=list(pools_to_fill_up)).forget()

    return {"action": "post", "session": session}


# http --json put http://localhost:8080/api/v1/sessions/2 active:=false
@router.put("/{id}", response_model=SessionResult, tags=["sessions"])
async def update_session(
    id: int,
    data: SessionUpdateModel,
    db_async_session: AsyncSession = Depends(req_db_async_session),
    tenant: Tenant = Depends(req_tenant),
):
    session = (
        await db_async_session.execute(
            select(Session)
            .filter_by(id=id)
            .options(
                selectinload(Session.tenant),
                selectinload(Session.session_nodes).selectinload(SessionNode.node),
            )
        )
    ).scalar_one_or_none()

    if not session:
        raise HTTPException(HTTP_404_NOT_FOUND)

    if not tenant.is_admin and session.tenant != tenant:
        raise HTTPException(HTTP_403_FORBIDDEN)

    if not session.active:
        raise HTTPException(HTTP_422_UNPROCESSABLE_ENTITY, f"session {id} is retired")

    if not data.active:
        session.active = data.active
        deprovision_nodes.delay(
            node_ids=[session_node.node_id for session_node in session.session_nodes]
        ).forget()

    await db_async_session.commit()

    return {"action": "put", "session": session}


# http delete http://localhost:8080/api/v1/sessions/2
@router.delete("/{id}", response_model=SessionResult, tags=["sessions"])
async def delete_session(
    id: int,
    db_async_session: AsyncSession = Depends(req_db_async_session),
    tenant: Tenant = Depends(req_tenant),
):
    """Delete the session with the specified **ID**."""
    if not tenant.is_admin:
        raise HTTPException(HTTP_403_FORBIDDEN)
    session = (
        await db_async_session.execute(
            select(Session)
            .filter_by(id=id)
            .options(
                selectinload(Session.tenant),
                selectinload(Session.session_nodes).selectinload(SessionNode.node),
            )
        )
    ).scalar_one_or_none()
    if not session:
        raise HTTPException(HTTP_404_NOT_FOUND)
    await db_async_session.delete(session)
    await db_async_session.commit()
    return {"action": "delete", "session": session}
