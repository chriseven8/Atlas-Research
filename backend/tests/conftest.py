import pytest

from financial_research.settings import Settings
from financial_research.storage import Repository


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        database_url="sqlite:///" + (tmp_path / "test.db").as_posix(),
        embed_worker=False,
        alpha_vantage_api_key="",
        openai_api_key="",
        openai_model="",
    )


@pytest.fixture
def repo(settings):
    repository = Repository(settings)
    repository.initialize()
    yield repository
    repository.close()
