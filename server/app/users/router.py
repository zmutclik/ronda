import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError

from app.auth import service as auth_service
from app.auth.dependencies import CurrentUserDep, require_csrf, require_permission
from app.auth.models import ROLE_LABELS, ROLES
from app.auth.permissions import PERMISSION_MATRIX, PERMISSIONS
from app.core.database import DbSession
from app.core.templating import render, templates
from app.users import service as user_service

router = APIRouter(prefix="/users", tags=["users"],
                   dependencies=[Depends(require_csrf), Depends(require_permission("users_manage"))])


async def _context(db, request: Request, tab: str) -> dict:
    p = request.query_params
    filters = {"q": (p.get("q") or "").strip()[:50], "role": p.get("role") if p.get("role") in ROLES else "",
               "active": p.get("active") if p.get("active") in ("1", "0") else ""}
    return {
        "tab": tab, "filters": filters,
        "users": await user_service.list_users(db, q=filters["q"] or None, role=filters["role"] or None,
                                               active=filters["active"] or None),
        "role_counts": await user_service.role_counts(db), "roles": ROLE_LABELS,
        "matrix": PERMISSION_MATRIX, "permissions": PERMISSIONS, "initials": user_service.initials,
    }


@router.get("")
async def users_page(request: Request, user: CurrentUserDep, db: DbSession, tab: str = "users"):
    tab = tab if tab in ("users", "roles") else "users"
    return render(request, "users/index.html", "users/_panel.html", await _context(db, request, tab))


async def _refresh(request: Request, db, message: str):
    return templates.TemplateResponse(
        request=request, name="users/_panel.html", context=await _context(db, request, "users"),
        headers={"HX-Trigger": json.dumps({"toast": message, "close-dialog": True})})


@router.post("")
async def create_user(request: Request, user: CurrentUserDep, db: DbSession):
    form = await request.form()
    username = str(form.get("username", "")).strip().lower()
    try:
        created = await auth_service.create_user(
            db, username, str(form.get("password", "")), str(form.get("role", "")),
            full_name=str(form.get("full_name", "")).strip(), must_change_password=True, actor=user.actor)
    except IntegrityError as err:
        raise HTTPException(status_code=422, detail=f"Username {username} sudah dipakai.") from err
    except ValueError as err:                       # InvalidPassword, role/nama tidak valid
        raise HTTPException(status_code=422, detail=str(err)) from err
    return await _refresh(request, db, f"User {created.username} dibuat. Password wajib diganti saat login pertama.")


@router.post("/{user_id}")
async def update_user(user_id: int, request: Request, user: CurrentUserDep, db: DbSession):
    form = await request.form()
    active = form.get("is_active") == "1"
    if user_id == user.id and (not active or form.get("role") != "admin"):
        raise HTTPException(status_code=422, detail="Tidak bisa menonaktifkan atau menurunkan role akun sendiri.")
    try:
        updated = await auth_service.update_user(
            db, user_id, full_name=str(form.get("full_name", "")).strip(), role=str(form.get("role", "")),
            is_active=active, actor=user.actor)
        new_password = str(form.get("new_password", ""))
        if new_password:
            await auth_service.reset_password(db, user_id, new_password, user.actor)
    except auth_service.UserNotFound as err:
        raise HTTPException(status_code=404) from err
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return await _refresh(request, db, f"User {updated.username} disimpan.")
