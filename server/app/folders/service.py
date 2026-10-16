import ipaddress
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import audit
from app.folders.models import Folder

SEP = " › "


@dataclass
class FolderNode:
    id: int
    name: str
    parent_id: int | None
    location: str | None
    subnet_hint: str | None
    sort_order: int
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


async def load_tree(db: AsyncSession) -> FolderTree:
    """Semua folder (puluhan baris) dalam satu query; susunan pohon dibuat di Python."""
    rows = (await db.execute(select(Folder).order_by(Folder.sort_order, Folder.name))).scalars().all()
    by_id = {
        r.id: FolderNode(r.id, r.name, r.parent_id, r.location, r.subnet_hint, r.sort_order) for r in rows
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
    actor: audit.Actor,
) -> Folder:
    _validate(name, location, subnet_hint)
    async with db.begin():
        if parent_id is not None:
            parent = await db.get(Folder, parent_id)
            if parent is None:
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
    actor: audit.Actor,
) -> None:
    _validate(name, location, subnet_hint)
    async with db.begin():
        folder = await db.get(Folder, folder_id, with_for_update=True)
        if folder is None:
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


async def delete_folder(db: AsyncSession, folder_id: int, *, host_count: int, actor: audit.Actor) -> None:
    """Hanya folder kosong (tanpa ruangan dan tanpa PC) yang bisa dihapus."""
    async with db.begin():
        folder = await db.get(Folder, folder_id, with_for_update=True)
        if folder is None:
            raise FolderError("Folder tidak ditemukan.")
        children = await db.scalar(select(func.count()).select_from(Folder).where(Folder.parent_id == folder_id))
        if children or host_count:
            raise FolderError("Folder masih berisi ruangan atau PC.")
        await db.delete(folder)
        await audit.record(db, actor, "pc", "Hapus folder", target_type="folder", target_id=folder_id,
                           target_label=folder.name)
