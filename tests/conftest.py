import os
import tempfile
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Ensure environment uses temp directory for test
test_db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["MYYARIS_DATA_DIR"] = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{test_db_file.name}"

from app.config import settings
from app.main import app
from app.repositories.database import Base, get_db, setup_sqlite_pragmas

test_engine = create_engine(
    f"sqlite:///{test_db_file.name}",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
setup_sqlite_pragmas(test_engine)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture(scope="session", autouse=True)
def setup_test_database():
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)
    if os.path.exists(test_db_file.name):
        try:
            os.remove(test_db_file.name)
        except OSError:
            pass


@pytest.fixture
def db_session():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def client(db_session):
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def phone_headers():
    return {"Authorization": f"Bearer {settings.PHONE_TOKEN}"}


@pytest.fixture
def car_headers():
    return {"Authorization": f"Bearer {settings.CAR_TOKEN}"}


@pytest.fixture
def admin_headers():
    return {"Authorization": f"Bearer {settings.ADMIN_TOKEN}"}
