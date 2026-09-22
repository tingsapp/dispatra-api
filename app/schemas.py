from typing import Annotated, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, EmailStr, field_validator

Name = Annotated[str, Field(min_length=1, max_length=160)]
LoginID = Annotated[str, Field(min_length=3, max_length=100, pattern=r'^[a-z0-9][a-z0-9@._+-]*$')]
Password = Annotated[str, Field(min_length=12, max_length=128)]
Slug = Annotated[str, Field(min_length=2, max_length=63, pattern=r'^[a-z0-9]+(?:-[a-z0-9]+)*$')]

class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')
    @field_validator('*', mode='before')
    @classmethod
    def trim_non_password(cls, value, info):
        return value.strip() if isinstance(value, str) and 'password' not in info.field_name else value

class Login(Input):
    organization: Slug | None = None
    portal: Literal['platform','dispatch','customer']
    login_id: LoginID
    password: Annotated[str, Field(min_length=1, max_length=128)]
    @field_validator('password', mode='before')
    @classmethod
    def exact_password(cls, value): return value

class OrganizationCreate(Input):
    slug: Slug
    name: Name
    admin_login: LoginID
    password: Password
    @field_validator('slug')
    @classmethod
    def reserved(cls, v):
        if v in {'api','platform','prototype','assets','health','login','customer','dispatch','www'}:
            raise ValueError('Choose another company identifier')
        return v

class OrganizationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    slug: str
    name: str
    active: bool

class ProfileInput(Input):
    contact_name: Annotated[str, Field(max_length=160)] = ''
    email: EmailStr | Literal[''] = ''
    phone: Annotated[str, Field(max_length=50)] = ''
    address: Annotated[str, Field(max_length=500)] = ''

class CustomerCreate(ProfileInput):
    number: Annotated[str, Field(min_length=1, max_length=50)]
    name: Name
    login_id: LoginID
    password: Password

class ProfileUpdate(ProfileInput):
    version: Annotated[int, Field(ge=1)]

class CustomerView(ProfileInput):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    number: str
    name: str
    status: str
    version: int

class CustomerAccessView(CustomerView):
    login_id: str

class PasswordChange(Input):
    current_password: Annotated[str, Field(min_length=1, max_length=128)]
    new_password: Password

class PasswordReset(Input):
    password: Password

class AccountView(BaseModel):
    id: UUID
    login_id: str
    role: Literal['PLATFORM_OWNER','DISPATCHER','CUSTOMER']
    organization: OrganizationView | None

class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict = Field(default_factory=dict)
    field_errors: list = Field(default_factory=list)
    retryable: bool = False
    request_id: str

class ErrorEnvelope(BaseModel):
    error: ErrorBody
