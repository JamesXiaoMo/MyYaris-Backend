from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field


class AuthenticatedUser(BaseModel):
    model_config = ConfigDict(extra="ignore")

    identity: str
    role: str  # user | admin | phone | car
    user_id: Optional[str] = None
    username: Optional[str] = None
    device_id: Optional[str] = None
    is_legacy_token: bool = False
    has_device_control: bool = False
    auth_via_cookie: bool = False


class UserRegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_-]+$", description="3-32 alphanumeric characters, dashes, or underscores")
    password: str = Field(..., min_length=8, max_length=128, description="Minimum 8 characters password")
    nickname: Optional[str] = Field(None, max_length=64)
    avatar_url: Optional[str] = Field(None, max_length=512)


class BootstrapAdminRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_-]+$")
    password: str = Field(..., min_length=8, max_length=128)
    nickname: Optional[str] = Field(None, max_length=64)


class UserLoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class UserResponse(BaseModel):
    id: str
    username: str
    nickname: Optional[str] = None
    avatar_url: Optional[str] = None
    role: str
    is_active: bool
    has_device_control: bool
    created_at: str
    updated_at: str
    last_login_at: Optional[str] = None


class LoginResponse(BaseModel):
    code: int = 200
    message: str = "Login successful"
    session_id: str
    token_type: str = "bearer"
    csrf_token: Optional[str] = None
    expires_at: str
    user: UserResponse


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)


class UpdateProfileRequest(BaseModel):
    nickname: Optional[str] = Field(None, max_length=64)
    avatar_url: Optional[str] = Field(None, max_length=512)


class DeviceBindingRequest(BaseModel):
    device_id: str = Field("car_p3pro", min_length=1, max_length=64)
    can_control: bool = True


class UserRoleUpdateRequest(BaseModel):
    role: str = Field(..., pattern=r"^(user|admin)$")


class UserListResponse(BaseModel):
    items: List[UserResponse]
    total: int
