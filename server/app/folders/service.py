import ipaddress
from dataclasses import dataclass, field

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import audit
from app.auth.models import User
from app.folders.models import Folder, FolderAccess

SEP = " › "


@dataclass
class FolderNode:
    id: int
    name: str
    parent_id: int | None
    location: str | None
    subnet_hint: str | None
    sort_order: int
    restricted: bool = False
    parent_name: str | None = None
    children: list["FolderNode"] = field(default_factory=list)

    @property
    def path(self) -> str:
        return f"{self.parent_name}{SEP}{self.name}" if self.parent_name else self.name

    @property
    def unit_id(self) -> int:
        return self.parent_id or self.id


@dataclass
class FolderTree:
    units: list[FolderNode]
    by_id: dict[int, FolderNode]

    def path(self, folder_id: int | None) -> str:
        node = self.by_id.get(folder_id) if folder_id else None
        return node.path if node else "Belum dikelompokkan"

    def subtree_ids(self, folder_id: int) -> list[int]:
        node = self.by_id[folder_id]
        return [node.id, *(c.id for c in node.children)]

    def options(self) -> list[FolderNode]:
        """Urutan untuk <select>: unit lalu ruangannya."""
        return [n for unit in self.units for n in (unit, *unit.children)]


# ---------- Folder terbatas ----------
# hidden = id folder yang TIDAK boleh dilihat user (dihitung sekali per request, folders/dependencies.py). Wajib
# diteruskan ke load_tree dan ke setiap query PC (hosts.service.visible): tanpa default, agar pemanggil baru tidak
# lupa dan diam-diam menampilkan semua folder.
NONE_HIDDEN: frozenset[int] = frozenset()


async def hidden_folder_ids(db: AsyncSession, user_id: int | None) -> frozenset[int]:
    """Folder terbatas yang user ini tidak punya aksesnya, ditambah ruangan di bawah unit yang tersembunyi.
    user_id None = sistem (worker, agent): melihat semua."""
    if user_id is None:
        return NONE_HIDDEN
    restricted = (await db.execute(select(Folder.id).where(Folder.is_restricted.is_(True)))).scalars().all()
    if not restricted:
        return NONE_HIDDEN
    allowed = set(await db.scalars(select(FolderAccess.folder_id).where(FolderAccess.user_id == user_id)))
    hidden_units = {f for f in restricted if f not in allowed}
    if not hidden_units:
        return NONE_HIDDEN
    children = await db.scalars(select(Folder.id).where(Folder.parent_id.in_(hidden_units)))
    return frozenset(hidden_units | set(children))


async def access_lists(db: AsyncSession) -> dict[int, set[int]]:
    """folder_id → user_id yang punya akses (untuk form folder terbatas)."""
    out: dict[int, set[int]] = {}
    for folder_id, user_id in (await db.execute(select(FolderAccess.folder_id, FolderAccess.user_id))).all():
        out.setdefault(folder_id, set()).add(user_id)
    return out


async def access_candidates(db: AsyncSession) -> list[User]:
    """User aktif yang bisa diberi akses ke folder terbatas."""
    return list(await db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.username)))


async def is_protected_user(db: AsyncSession, user_id: int, hidden: frozenset[int]) -> bool:
    """User yang memegang akses ke folder yang tersembunyi bagi pengubah (hidden) tidak boleh diubah olehnya:
    tanpa ini admin lain bisa mereset password pemegang akses lalu login sebagai dia."""
    if not hidden:
        return False
    return bool(await db.scalar(select(func.count()).select_from(FolderAccess).where(
        FolderAccess.user_id == user_id, FolderAccess.folder_id.in_(hidden))))


async def set_access(
    db: AsyncSession, folder_id: int, *, restricted: bool, user_ids: list[int], hidden: frozenset[int],
    actor: audit.Actor,
) -> None:
    """Ubah status terbatas dan daftar akses. Pengubah selalu ikut di daftar (tidak bisa mengunci diri sendiri
    keluar); hanya user yang bisa melihat folder ini yang bisa mengubahnya."""
    if folder_id in hidden:
        raise FolderError("Folder tidak ditemukan.")
    async with db.begin():
        folder = await db.get(Folder, folder_id, with_for_update=True)
        if folder is None:
            raise FolderError("Folder tidak ditemukan.")
        wanted = set(user_ids) | {actor.user_id} if restricted else set()
        valid = set(await db.scalars(select(User.id).where(User.id.in_(wanted)))) if wanted else set()
        before = set(await db.scalars(select(FolderAccess.user_id).where(FolderAccess.folder_id == folder_id)))
        if folder.is_restricted == restricted and before == valid:
            return
        await db.execute(delete(FolderAccess).where(FolderAccess.folder_id == folder_id))
        if valid:
            await db.execute(insert(FolderAccess).values([{"folder_id": folder_id, "user_id": u} for u in valid]))
        folder.is_restricted = restricted
        names = sorted(await db.scalars(select(User.username).where(User.id.in_(valid)))) if valid else []
        await audit.record(db, actor, "pc", "Ubah akses folder", sensitive=True, target_type="folder",
                           target_id=folder_id, target_label=folder.name,
                           detail=f"Terbatas · akses: {', '.join(names)}" if restricted else "Tidak terbatas lagi")


async def load_tree(db: AsyncSession, *, hidden: frozenset[int]) -> FolderTree:
    """Folder yang boleh dilihat (puluhan baris) dalam satu query; susunan pohon dibuat di Python."""
    stmt = select(Folder).order_by(Folder.sort_order, Folder.name)
    if hidden:
        stmt = stmt.where(Folder.id.not_in(hidden))
    rows = (await db.execute(stmt)).scalars().all()
    by_id = {
        r.id: FolderNode(r.id, r.name, r.parent_id, r.location, r.subnet_hint, r.sort_order, r.is_restricted)
        for r in rows
    }
    units = []
    for node in by_id.values():
        parent = by_id.get(node.parent_id) if node.parent_id else None
        if parent:
            node.parent_name = parent.name
            parent.children.append(node)
        else:
            units.append(node)
    return FolderTree(units=units, by_id=by_id)


def suggest(tree: FolderTree, ip: str | None) -> FolderNode | None:
    """Folder dengan subnet_hint paling spesifik yang memuat IP (untuk PC baru)."""
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    best: tuple[int, FolderNode] | None = None
    for node in tree.by_id.values():
        if not node.subnet_hint:
            continue
        try:
            net = ipaddress.ip_network(node.subnet_hint, strict=False)
        except ValueError:
            continue
        if addr.version == net.version and addr in net and (best is None or net.prefixlen > best[0]):
            best = (net.prefixlen, node)
    return best[1] if best else None


class FolderError(ValueError):
    pass


def _validate(name: str, location: str | None, subnet_hint: str | None) -> None:
    if not 0 < len(name) <= 80:
        raise FolderError("Nama folder harus 1–80 karakter.")
    if location and len(location) > 120:
        raise FolderError("Lokasi maksimal 120 karakter.")
    if subnet_hint:
        try:
            ipaddress.ip_network(subnet_hint, strict=False)
        except ValueError as err:
            raise FolderError("Subnet harus format CIDR, mis. 10.10.12.0/24.") from err


async def create_folder(
    db: AsyncSession, *, name: str, parent_id: int | None, location: str | None, subnet_hint: str | None,
    actor: audit.Actor, hidden: frozenset[int],
) -> Folder:
    _validate(name, location, subnet_hint)
    async with db.begin():
        if parent_id is not None:
            parent = await db.get(Folder, parent_id)
            if parent is None or parent_id in hidden:
                raise FolderError("Folder induk tidak ditemukan.")
            if parent.parent_id is not None:
                raise FolderError("Folder maksimal 2 tingkat (Unit › Ruangan).")
        # UNIQUE (parent_id, name) tidak berlaku untuk unit (parent_id NULL): cek di sini
        same = select(func.count()).select_from(Folder).where(
            Folder.name == name,
            Folder.parent_id.is_(None) if parent_id is None else Folder.parent_id == parent_id,
        )
        if await db.scalar(same):
            raise FolderError("Nama folder sudah dipakai di tingkat ini.")
        folder = Folder(name=name, parent_id=parent_id, location=location or None, subnet_hint=subnet_hint or None)
        db.add(folder)
        await db.flush()
        await db.refresh(folder)
        await audit.record(db, actor, "pc", "Tambah folder", target_type="folder", target_id=folder.id,
                           target_label=name)
    return folder


async def update_folder(
    db: AsyncSession, folder_id: int, *, name: str, location: str | None, subnet_hint: str | None,
    actor: audit.Actor, hidden: frozenset[int],
) -> None:
    _validate(name, location, subnet_hint)
    async with db.begin():
        folder = await db.get(Folder, folder_id, with_for_update=True)
        if folder is None or folder_id in hidden:
            raise FolderError("Folder tidak ditemukan.")
        same = select(func.count()).select_from(Folder).where(
            Folder.name == name, Folder.id != folder_id,
            Folder.parent_id.is_(None) if folder.parent_id is None else Folder.parent_id == folder.parent_id,
        )
        if await db.scalar(same):
            raise FolderError("Nama folder sudah dipakai di tingkat ini.")
        old = folder.name
        folder.name, folder.location, folder.subnet_hint = name, location or None, subnet_hint or None
        await audit.record(db, actor, "pc", "Ubah folder", target_type="folder", target_id=folder_id,
                           target_label=name, detail=f"{old} → {name}" if old != name else None)


async def delete_folder(
    db: AsyncSession, folder_id: int, *, host_count: int, actor: audit.Actor, hidden: frozenset[int],
) -> None:
    """Hanya folder kosong (tanpa ruangan dan tanpa PC) yang bisa dihapus."""
    async with db.begin():
        folder = await db.get(Folder, folder_id, with_for_update=True)
        if folder is None or folder_id in hidden:
            raise FolderError("Folder tidak ditemukan.")
        children = await db.scalar(select(func.count()).select_from(Folder).where(Folder.parent_id == folder_id))
        if children or host_count:
            raise FolderError("Folder masih berisi ruangan atau PC.")
        await db.delete(folder)
        await audit.record(db, actor, "pc", "Hapus folder", target_type="folder", target_id=folder_id,
                           target_label=folder.name)
