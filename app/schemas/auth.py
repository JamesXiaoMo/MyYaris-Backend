from typing import Optional
from pydantic import BaseModel


class AuthenticatedUser(BaseModel):
    identity: str
    role: str  # car | phone | admin
    device_id: Optional[str] = None
