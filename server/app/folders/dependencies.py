"""Folder yang tersembunyi untuk user yang sedang login (folder terbatas tanpa akses)."""
from typing import Annotated

from fastapi import Depends

from app.auth.dependencies import CurrentUserDep
from app.core.database import async_session_factory
from app.folders.service import hidden_folder_ids


async def get_hidden(user: CurrentUserDep) -> frozenset[int]:
    # Session DB sendiri (seperti get_current_user): query di session request memicu autobegin sehingga
    # db.begin() di service gagal.
    async with async_session_factory() as db, db.begin():
        return await hidden_folder_ids(db, user.id)


HiddenDep = Annotated[frozenset[int], Depends(get_hidden)]
