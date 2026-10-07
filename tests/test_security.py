import pytest
from router.security import validate_provider_base_url

def test_accepts_public_https_provider_base():
    assert validate_provider_base_url("https://api.example.com/v1/") == "https://api.example.com/v1"

@pytest.mark.parametrize("url", [
    "http://api.example.com/v1",
    "https://localhost/v1",
    "https://127.0.0.1/v1",
    "https://10.0.0.2/v1",
    "https://provider.internal/v1",
    "https://api.example.com/v1/models",
    "https://user:pass@api.example.com/v1",
])
def test_rejects_unsafe_provider_bases(url):
    with pytest.raises(ValueError):
        validate_provider_base_url(url)
