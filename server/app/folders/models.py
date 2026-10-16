from datetime import datetime

from sqlalchemy import ForeignKey, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, UInt, USmallInt


class Folder(Base):
    """Folder Unit › Ruangan dalam satu tabel. parent_id NULL = unit; maksimal 2 tingkat (dijaga service).
    PC dengan folder_id NULL = "Belum dikelompokkan"."""

    __tablename__ = "folders"
    __table_args__ = (UniqueConstraint("parent_id", "name", name="uq_folders_parent_name"),)

    id: Mapped[int] = mapped_column(UInt, primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(UInt, ForeignKey("folders.id", name="fk_folders_parent"))
    name: Mapped[str] = mapped_column(String(80))
    location: Mapped[str | None] = mapped_column(String(120))
    subnet_hint: Mapped[str | None] = mapped_column(String(43))       # CIDR untuk saran folder PC baru
    sort_order: Mapped[int] = mapped_column(USmallInt, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
