from router.web_search import needs_current_search, search_current, search_context


def test_current_query_detection():
    assert needs_current_search("¿Cuándo juega River?")
    assert needs_current_search("¿Hay F1 este finde?")
    assert needs_current_search("¿Cuánto está el dólar hoy?")
    assert not needs_current_search("Explicame qué es una función de Python")


def test_search_extracts_source_and_rejects_unsafe_url(monkeypatch):
    xml = (b"<rss><channel>"
           b"<item><title>River - proximo partido</title>"
           b"<link>https://example.org/river</link>"
           b"<description>Domingo 18:00</description><pubDate>Sat, 10 Oct 2026 12:00:00 GMT</pubDate></item>"
           b"<item><title>Bad</title><link>http://unsafe.example</link></item>"
           b"</channel></rss>")
    class FakeResponse:
        content = xml
        def raise_for_status(self):
            return None
    seen = []
    def fake_get(url, **kwargs):
        seen.append((url, kwargs))
        return FakeResponse()
    monkeypatch.setattr("router.web_search.httpx.get", fake_get)
    found = search_current("Cuándo juega River", limit=1)
    assert len(found) == 1
    assert found[0]["url"] == "https://example.org/river"
    assert seen[0][1]["timeout"] <= 6.0


def test_search_unavailable_never_fabricates(monkeypatch):
    monkeypatch.setattr("router.web_search.search_current", lambda task, **kwargs: [])
    context = search_context("Cuándo juega River")
    assert "No se obtuvieron resultados" in context
    assert "No inventes" in context


def test_search_context_marks_external_text_untrusted(monkeypatch):
    monkeypatch.setattr("router.web_search.search_current", lambda task, **kwargs: [
        {"title": "Partido", "url": "https://example.org", "published": "", "snippet": "Texto"}])
    assert "NO CONFIABLE" in search_context("River")
