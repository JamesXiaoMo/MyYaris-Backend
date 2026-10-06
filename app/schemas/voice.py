from typing import Optional
from pydantic import BaseModel


class VoiceUploadResponse(BaseModel):
    request_id: str
    status: str
    message: str
