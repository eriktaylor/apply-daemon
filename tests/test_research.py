"""Tests for the deep research module."""

from unittest.mock import MagicMock, patch

from src.research import _chunk_text, run_deep_research


class TestGracefulDegradation:
    """Verify the module catches errors and returns empty string."""

    @patch(
        "src.research._run_deep_research_inner",
        side_effect=TimeoutError("connection timed out"),
    )
    def test_network_timeout_returns_empty(self, mock_inner):
        result = run_deep_research("Acme Corp", "Backend engineer role")
        assert result == ""

    @patch("src.research._run_deep_research_inner", side_effect=ConnectionError("refused"))
    def test_connection_error_returns_empty(self, mock_inner):
        result = run_deep_research("Acme Corp", "Backend engineer role")
        assert result == ""

    @patch("src.research._run_deep_research_inner", side_effect=Exception("unexpected"))
    def test_generic_exception_returns_empty(self, mock_inner):
        result = run_deep_research("Acme Corp", "Backend engineer role")
        assert result == ""


class TestDeepResearchAlwaysRuns:
    """Deep research is always enabled — no bypass flag exists."""

    @patch("src.tailor.run_deep_research", return_value="Research context")
    def test_research_called_for_listing_with_company(self, mock_run_research):
        """build_prompt should always call run_deep_research when company is set."""
        with patch("src.tailor.Database") as MockDB, \
             patch("src.tailor.load_profile") as mock_profile, \
             patch("src.tailor.read_dropzone_file", return_value=None):

            db_instance = MagicMock()
            row = MagicMock()
            row.__getitem__ = lambda self, key: {
                "title": "Eng", "company": "Co", "location": "Remote",
                "salary": "", "job_summary": "desc", "reason": "reason",
            }.get(key, "")
            row.keys.return_value = [
                "title", "company", "location", "salary", "job_summary", "reason",
            ]
            db_instance.get_listing_by_id.return_value = row
            MockDB.return_value.__enter__ = MagicMock(return_value=db_instance)
            MockDB.return_value.__exit__ = MagicMock(return_value=False)

            mock_profile.return_value = {
                "name": "Test",
                "llm_context": "profile text",
                "settings": {"generate_assets": ["resume"]},
            }

            from src.tailor import build_prompt
            build_prompt("test-id-123")

            mock_run_research.assert_called_once()


class TestChunking:
    """Verify text chunking handles edge cases."""

    def test_empty_string(self):
        assert _chunk_text("") == []

    def test_small_text_single_chunk(self):
        text = "hello world foo bar"
        chunks = _chunk_text(text, chunk_size=10)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_exact_boundary(self):
        words = ["word"] * 10
        text = " ".join(words)
        chunks = _chunk_text(text, chunk_size=5)
        assert len(chunks) == 2
        assert chunks[0] == "word word word word word"
        assert chunks[1] == "word word word word word"

    def test_massive_repetitive_input(self):
        """5MB of repetitive text should not cause OOM or index errors."""
        # ~5MB of text (each word ~5 chars + space = 6 bytes, ~830K words)
        text = "lorem " * 830_000
        chunks = _chunk_text(text, chunk_size=500)
        assert len(chunks) > 1000
        # Every chunk should have content
        for chunk in chunks:
            assert len(chunk.strip()) > 0

    def test_single_word(self):
        chunks = _chunk_text("hello", chunk_size=500)
        assert len(chunks) == 1
        assert chunks[0] == "hello"


class TestSearchAndScrape:
    """Test search and scrape with mocked network calls."""

    def test_duckduckgo_search_returns_urls(self):
        mock_ddgs = MagicMock()
        mock_ddgs.__enter__ = MagicMock(return_value=mock_ddgs)
        mock_ddgs.__exit__ = MagicMock(return_value=False)
        mock_ddgs.text.return_value = [
            {"href": "https://example.com/page1"},
            {"href": "https://example.com/page2"},
        ]
        mock_module = MagicMock()
        mock_module.DDGS.return_value = mock_ddgs
        with patch.dict("sys.modules", {"ddgs": mock_module}):
            from src.research import _search_duckduckgo
            urls = _search_duckduckgo("test query")
        assert urls == ["https://example.com/page1", "https://example.com/page2"]

    def test_duckduckgo_search_exception_returns_empty(self):
        mock_module = MagicMock()
        mock_module.DDGS.side_effect = Exception("network error")
        with patch.dict("sys.modules", {"ddgs": mock_module}):
            from src.research import _search_duckduckgo
            urls = _search_duckduckgo("test query")
        assert urls == []

    def test_scrape_url_returns_empty_on_failure(self):
        with patch.dict("sys.modules", {"trafilatura": MagicMock()}):
            import sys
            mock_traf = sys.modules["trafilatura"]
            mock_traf.fetch_url.return_value = None
            from src.research import _scrape_url
            result = _scrape_url("https://example.com")
            assert result == ""


class TestSearchPacing:
    """Verify inter-query sleep is called between DuckDuckGo searches."""

    @patch("src.research._scrape_url", return_value="")
    @patch("src.research._search_duckduckgo", return_value=[])
    @patch("src.research._generate_search_queries")
    @patch("src.research._get_openrouter_config", return_value=("fake-key", "fake-model"))
    @patch("src.research.time")
    def test_pacing_fires_between_queries(
        self, mock_time, mock_config, mock_gen_queries, mock_search, mock_scrape,
    ):
        """With 3 queries, sleep(2) should be called exactly 2 times (between queries)."""
        mock_gen_queries.return_value = ["query1", "query2", "query3"]

        from src.research import _run_deep_research_inner
        # Will return "" because no content is scraped, but pacing should still fire
        _run_deep_research_inner("Acme Corp", "Backend engineer role")

        assert mock_time.sleep.call_count == 2
        mock_time.sleep.assert_called_with(2)

    @patch("src.research._scrape_url", return_value="")
    @patch("src.research._search_duckduckgo", return_value=[])
    @patch("src.research._generate_search_queries")
    @patch("src.research._get_openrouter_config", return_value=("fake-key", "fake-model"))
    @patch("src.research.time")
    def test_single_query_no_pacing(
        self, mock_time, mock_config, mock_gen_queries, mock_search, mock_scrape,
    ):
        """With only 1 query, no sleep should be called."""
        mock_gen_queries.return_value = ["query1"]

        from src.research import _run_deep_research_inner
        _run_deep_research_inner("Acme Corp", "Backend engineer role")

        mock_time.sleep.assert_not_called()


class TestSynthesisPipeline:
    """Test end-to-end synthesis with mocked network calls."""

    MOCK_SCRAPED_CONTENT = (
        "Our engineering team at ExampleCorp uses Python, PyTorch, and a heavily "
        "customized Kubernetes cluster. We deploy to AWS EKS with Terraform. "
        "ExampleCorp was founded in 2020 and recently raised a $50M Series B. "
        "The team values async communication and deep work."
    )

    @patch("src.research._openrouter_generate")
    @patch("src.research._scrape_url")
    @patch("src.research._search_duckduckgo")
    @patch("src.research._generate_search_queries")
    @patch("src.research._get_openrouter_config", return_value=("fake-key", "fake-model"))
    @patch("src.research.time")
    def test_synthesis_contains_key_terms(
        self, mock_time, mock_config, mock_gen_queries, mock_search,
        mock_scrape, mock_generate,
    ):
        """Mock the full pipeline and verify synthesis output contains key terms."""
        mock_gen_queries.return_value = ["ExampleCorp tech stack"]
        mock_search.return_value = ["https://example.com/eng-blog"]
        mock_scrape.return_value = self.MOCK_SCRAPED_CONTENT

        mock_generate.return_value = (
            "## ExampleCorp Research Brief\n\n"
            "ExampleCorp uses Python and PyTorch for ML workloads, "
            "deployed on Kubernetes via AWS EKS. Recently raised $50M Series B."
        )

        from src.research import _run_deep_research_inner
        result = _run_deep_research_inner("ExampleCorp", "ML engineer role")

        assert "Python" in result
        assert "PyTorch" in result
        assert result != ""

    @patch("src.research._openrouter_generate")
    @patch("src.research._scrape_url")
    @patch("src.research._search_duckduckgo")
    @patch("src.research._generate_search_queries")
    @patch("src.research._get_openrouter_config", return_value=("fake-key", "fake-model"))
    @patch("src.research.time")
    def test_synthesis_returns_empty_when_no_scrape(
        self, mock_time, mock_config, mock_gen_queries, mock_search,
        mock_scrape, mock_generate,
    ):
        """If scraping returns nothing, synthesis should return empty string."""
        mock_gen_queries.return_value = ["query1"]
        mock_search.return_value = ["https://example.com/page"]
        mock_scrape.return_value = ""  # Nothing scraped

        from src.research import _run_deep_research_inner
        result = _run_deep_research_inner("GhostCorp", "Backend role")

        assert result == ""
        mock_generate.assert_not_called()
