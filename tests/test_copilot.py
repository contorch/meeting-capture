from pathlib import Path

from meeting_capture import copilot as c


class TestTrigger:
    def test_questions_fire(self):
        assert c.is_trigger("What did we decide about pricing?")
        assert c.is_trigger("Did we ship the retry fix yet")
        assert c.is_trigger("remind me what the rate limit was")

    def test_statements_dont_fire(self):
        assert not c.is_trigger("Sounds good, thanks.")
        assert not c.is_trigger("Okay.")

    def test_too_short_ignored(self):
        assert not c.is_trigger("what?")

    def test_commitment_reference_fires(self):
        assert c.is_trigger("last time you promised this would be behind a flag")


class TestKeywords:
    def test_drops_stopwords_and_dupes(self):
        kw = c.keywords("What did we decide about the pricing pricing model?")
        assert "pricing" in kw and "model" in kw
        assert "the" not in kw and "what" not in kw
        assert kw.count("pricing") == 1


class TestRetrieval:
    def _write(self, d: Path, stem: str, *lines: str):
        (d / f"{stem}.md").write_text("\n".join(lines), encoding="utf-8")

    def test_scores_by_matching_terms(self, tmp_path):
        self._write(tmp_path, "meeting-2026-07-08T10-00-00",
                    "[14:31] **Me:** pricing is usage-based, annual discount parked until Q4")
        self._write(tmp_path, "meeting-2026-06-01T09-00-00",
                    "[09:10] **Them:** the weather was nice")
        snips = c.retrieve_transcripts("what did we decide about pricing and the discount",
                                       transcripts_dir=tmp_path)
        assert snips and "usage-based" in snips[0].text
        assert snips[0].source.startswith("meeting-2026-07-08")

    def test_excludes_current_session(self, tmp_path):
        self._write(tmp_path, "meeting-current",
                    "[10:00] **Them:** pricing pricing discount discount")
        assert c.retrieve_transcripts("pricing discount", exclude_stem="meeting-current",
                                      transcripts_dir=tmp_path) == []

    def test_ranks_more_matches_first(self, tmp_path):
        self._write(tmp_path, "meeting-a", "[10:00] **Me:** pricing discount decided today")
        self._write(tmp_path, "meeting-b", "[10:00] **Me:** pricing was mentioned once")
        snips = c.retrieve_transcripts("what did we decide about pricing and the discount",
                                       transcripts_dir=tmp_path)
        assert snips[0].source == "meeting-a"  # 3 matches ranks above 1 match

    def test_no_match_is_empty(self, tmp_path):
        self._write(tmp_path, "m", "[10:00] **Me:** entirely unrelated content")
        assert c.retrieve_transcripts("pricing discount", transcripts_dir=tmp_path) == []

    def test_window_carries_the_answer(self, tmp_path):
        # The answer follows the question on the next line and shares none of
        # the query keywords — the window must bring it along.
        self._write(tmp_path, "meeting-x",
                    "# header",
                    "[14:34] **Them:** and the API rate limit for the Acme integration?",
                    "[14:34] **Me:** hard cap is 50 requests per second, do not exceed it")
        snips = c.retrieve_transcripts("what is the rate limit on the Acme API", transcripts_dir=tmp_path)
        assert snips and "50 requests per second" in snips[0].text


class TestConsider:
    def test_non_trigger_returns_none(self):
        assert c.consider("Thanks, bye.", []) is None

    def test_whisper_from_injected_llm(self):
        w = c.consider(
            "What did we decide about pricing?",
            ["Them: hi", "Me: hey"],
            retriever=lambda q, ex: [c.Snippet("meeting-2026-07-08", "pricing usage-based, discount til Q4")],
            llm=lambda prompt, model: "Usage-based; annual discount parked until Q4. (meeting-2026-07-08)",
        )
        assert w and "Usage-based" in w["text"]
        assert w["sources"] == ["meeting-2026-07-08"]

    def test_none_from_llm_is_silence(self):
        w = c.consider("What did we decide about pricing?", [],
                       retriever=lambda q, ex: [], llm=lambda p, m: "NONE")
        assert w is None

    def test_llm_error_is_silence(self):
        def boom(p, m):
            raise RuntimeError("api down")
        w = c.consider("What did we decide?", [], retriever=lambda q, ex: [], llm=boom)
        assert w is None

    def test_build_prompt_includes_parts(self):
        p = c.build_prompt("What about pricing?", ["Me: hi"], [c.Snippet("m1", "pricing decided")])
        assert "What about pricing?" in p and "pricing decided" in p and "Me: hi" in p
