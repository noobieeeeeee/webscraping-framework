from __future__ import annotations

import unittest

from price_extractor.browser_readiness import (
    DEFAULT_GERMAN_PRINTING_KEYWORDS,
    attempt_cookie_consent,
    trigger_configurator_via_semantic_labels,
    wait_for_configurator_readiness,
)


class _FakeHandle:
    def __init__(self, *, visible: bool = True, click_raises: bool = False) -> None:
        self._visible = visible
        self._click_raises = click_raises
        self.clicked = False

    def is_visible(self) -> bool:
        return self._visible

    def click(self, timeout: int = 0) -> None:
        _ = timeout
        if self._click_raises:
            raise RuntimeError("click failed")
        self.clicked = True


class _FakeElement:
    def __init__(
        self,
        *,
        text: str = "",
        value: str | None = None,
        click_raises: bool = False,
    ) -> None:
        self._text = text
        self._value = value
        self._click_raises = click_raises
        self.clicked = False

    def inner_text(self) -> str:
        return self._text

    def get_attribute(self, key: str) -> str | None:
        if key == "value":
            return self._value
        return None

    def click(self, timeout: int = 0) -> None:
        _ = timeout
        if self._click_raises:
            raise RuntimeError("click failed")
        self.clicked = True


class _FakeFrame:
    def __init__(
        self,
        *,
        url: str,
        selectors: dict[str, _FakeHandle] | None = None,
        elements: list[_FakeElement] | None = None,
    ) -> None:
        self.url = url
        self._selectors = selectors or {}
        self._elements = elements or []

    def query_selector(self, selector: str):
        return self._selectors.get(selector)

    def query_selector_all(self, selector: str):
        _ = selector
        return list(self._elements)


class _FakePage:
    def __init__(
        self,
        *,
        frames: list[_FakeFrame] | None = None,
        evaluate_values: list = None,
        raise_on_wait: bool = False,
    ) -> None:
        self.frames = frames or []
        self._evaluate_values = evaluate_values if evaluate_values is not None else [0]
        self._evaluate_index = 0
        self._raise_on_wait = raise_on_wait
        self.wait_calls: list[int] = []
        self.evaluate_scripts: list[str] = []

    def evaluate(self, script: str):
        self.evaluate_scripts.append(script)
        if self._evaluate_index < len(self._evaluate_values):
            value = self._evaluate_values[self._evaluate_index]
            self._evaluate_index += 1
            return value
        return self._evaluate_values[-1]

    def wait_for_timeout(self, ms: int) -> None:
        self.wait_calls.append(ms)
        if self._raise_on_wait:
            raise RuntimeError("wait failed")


class TestBrowserReadiness(unittest.TestCase):
    def test_attempt_cookie_consent_disabled(self) -> None:
        page = _FakePage(frames=[])
        state = attempt_cookie_consent(page=page, auto_accept_cookies=False)

        self.assertFalse(state["attempted"])
        self.assertFalse(state["bannerDetected"])
        self.assertFalse(state["clicked"])

    def test_attempt_cookie_consent_clicks_selector(self) -> None:
        handle = _FakeHandle(visible=True)
        frame = _FakeFrame(
            url="https://example.test/frame",
            selectors={"button#onetrust-accept-btn-handler": handle},
        )
        page = _FakePage(frames=[frame])

        state = attempt_cookie_consent(page=page, auto_accept_cookies=True)

        self.assertTrue(state["attempted"])
        self.assertTrue(state["bannerDetected"])
        self.assertTrue(state["clicked"])
        self.assertEqual(state["matchedSelector"], "button#onetrust-accept-btn-handler")
        self.assertEqual(state["frameUrl"], "https://example.test/frame")

    def test_attempt_cookie_consent_falls_back_to_text_match(self) -> None:
        element = _FakeElement(text="Alle akzeptieren")
        frame = _FakeFrame(url="https://example.test/frame", selectors={}, elements=[element])
        page = _FakePage(frames=[frame])

        state = attempt_cookie_consent(page=page, auto_accept_cookies=True)

        self.assertTrue(state["bannerDetected"])
        self.assertTrue(state["clicked"])
        self.assertIn("akzeptieren", str(state["matchedText"]).lower())
        self.assertEqual(state["frameUrl"], "https://example.test/frame")

    def test_attempt_cookie_consent_shadow_dom_walker_clicks(self) -> None:
        # Per-frame selectors find nothing. Shadow-DOM walker reports a click.
        page = _FakePage(
            frames=[],
            evaluate_values=[{
                "clicked": True,
                "text": "Alle akzeptieren",
                "bannerDetected": True,
                "frameUrl": "https://example.test/page",
            }],
        )

        state = attempt_cookie_consent(page=page, auto_accept_cookies=True)

        self.assertTrue(state["attempted"])
        self.assertTrue(state["bannerDetected"])
        self.assertTrue(state["clicked"])
        self.assertEqual(state["matchedSelector"], "shadow_dom_walker")
        self.assertIn("akzeptieren", str(state["matchedText"]).lower())
        self.assertEqual(state["frameUrl"], "https://example.test/page")

    def test_attempt_cookie_consent_shadow_dom_walker_reports_banner_only(self) -> None:
        # Shadow-DOM walker finds a banner host but no clickable accept button.
        page = _FakePage(
            frames=[],
            evaluate_values=[{
                "clicked": False,
                "bannerDetected": True,
                "frameUrl": "https://example.test/page",
            }],
        )

        state = attempt_cookie_consent(page=page, auto_accept_cookies=True)

        self.assertTrue(state["bannerDetected"])
        self.assertFalse(state["clicked"])
        self.assertIsNone(state["matchedSelector"])

    def test_attempt_cookie_consent_shadow_dom_walker_tolerates_eval_failure(self) -> None:
        # Walker raises -> consent helper still returns a sane state object.
        class _RaisingPage(_FakePage):
            def evaluate(self, script: str):
                raise RuntimeError("eval failed")

        page = _RaisingPage(frames=[])
        state = attempt_cookie_consent(page=page, auto_accept_cookies=True)

        self.assertTrue(state["attempted"])
        self.assertFalse(state["clicked"])
        self.assertFalse(state["bannerDetected"])

    def test_wait_for_configurator_readiness_script_dedupes_via_set(self) -> None:
        # Sanity check on the readiness script wiring: the script uses a Set to dedupe
        # elements that match multiple widened selectors. We can't run the JS here, but
        # we can assert the script body includes the widened selectors so future edits
        # don't silently revert the broadening.
        page = _FakePage(evaluate_values=[5])

        readiness = wait_for_configurator_readiness(
            page=page,
            request_urls=[],
            timeout_ms=10000,
        )

        self.assertEqual(readiness["reason"], "interactive_controls")
        self.assertGreaterEqual(int(readiness["interactiveCount"]), 2)
        last_script = page.evaluate_scripts[-1]
        for token in ['role="radio"', 'role="checkbox"', 'role="tab"', 'data-testid', 'aria-selected']:
            self.assertIn(token, last_script, f"missing widened selector token: {token}")

    def test_wait_for_configurator_readiness_prefers_interactive_controls(self) -> None:
        page = _FakePage(evaluate_values=[3])

        readiness = wait_for_configurator_readiness(
            page=page,
            request_urls=[],
            timeout_ms=10000,
        )

        self.assertEqual(readiness["reason"], "interactive_controls")
        self.assertEqual(readiness["interactiveCount"], 3)
        self.assertEqual(readiness["apiHits"], 0)
        self.assertEqual(readiness["waitedMs"], 0)

    def test_wait_for_configurator_readiness_invokes_consent_retry_callback(self) -> None:
        # Simulate readiness that idles (0 controls, 0 api hits) until it times out.
        # Use a short timeout so the test runs fast; verify the consent retry callback
        # was invoked at least once during the wait.
        page = _FakePage(evaluate_values=[0])

        calls: list[int] = []

        def retry(p) -> None:
            calls.append(1)

        readiness = wait_for_configurator_readiness(
            page=page,
            request_urls=[],
            timeout_ms=4000,
            consent_retry_callback=retry,
            consent_retry_interval_ms=1000,
        )

        self.assertEqual(readiness["reason"], "timeout")
        self.assertGreater(len(calls), 0, "consent retry callback should have been invoked")
        self.assertEqual(readiness["consentRetriesAttempted"], len(calls))
        self.assertGreaterEqual(int(readiness["maxWaitMs"]), 2000)

    def test_wait_for_configurator_readiness_max_wait_scales_with_timeout(self) -> None:
        # The old cap was min(12000, timeout_ms // 2). The new cap is min(timeout_ms, 30000).
        # Lock that in so a future regression doesn't silently re-cap us at 12s.
        page = _FakePage(evaluate_values=[0])
        readiness = wait_for_configurator_readiness(
            page=page,
            request_urls=[],
            timeout_ms=20000,
        )
        # With timeout_ms=20000 and no controls / no api hits, the loop should have
        # waited up to ~20000ms (cap of min(timeout_ms, 30000) == 20000).
        self.assertGreaterEqual(int(readiness["maxWaitMs"]), 20000)

    def test_wait_for_configurator_readiness_uses_api_activity_signal(self) -> None:
        page = _FakePage(evaluate_values=[0])
        request_urls = [
            "https://api.saxoprint.de/product-configuration/get-product-values",
            "https://example.test/api/calculation",
        ]

        readiness = wait_for_configurator_readiness(
            page=page,
            request_urls=request_urls,
            timeout_ms=10000,
        )

        self.assertEqual(readiness["reason"], "pricing_api_activity")
        self.assertEqual(readiness["interactiveCount"], 0)
        self.assertGreaterEqual(int(readiness["apiHits"]), 2)
        self.assertEqual(readiness["waitedMs"], 0)


class TestSemanticLabelProbe(unittest.TestCase):
    def test_probe_returns_diagnostics_and_records_delta(self) -> None:
        # Simulate: page.evaluate returns one detected group and one click.
        # Then the request_urls list "grows" between before/after measurements.
        request_urls = ["https://example.test/static.css", "https://example.test/page.html"]

        class _ProbePage(_FakePage):
            def evaluate(self, script):
                self.evaluate_scripts.append(script)
                request_urls.append("https://api.example.test/api/productDetails/12345")
                request_urls.append("https://api.example.test/api/calculation/price")
                return {
                    "groupsDetected": [
                        {"group": "menge", "matchedKeyword": "Auflage", "labelText": "Auflage"},
                    ],
                    "clicksPerformed": [
                        {
                            "group": "menge",
                            "matchedKeyword": "Auflage",
                            "labelText": "Auflage",
                            "clickedTag": "button",
                            "clickedValue": "250",
                        },
                    ],
                }

        page = _ProbePage()
        result = trigger_configurator_via_semantic_labels(
            page=page,
            request_urls=request_urls,
        )

        self.assertTrue(result["attempted"])
        self.assertEqual(len(result["groupsDetected"]), 1)
        self.assertEqual(result["groupsDetected"][0]["group"], "menge")
        self.assertEqual(len(result["clicksPerformed"]), 1)
        self.assertEqual(result["requestCountBefore"], 2)
        self.assertEqual(result["requestCountAfter"], 4)
        self.assertEqual(result["requestCountDelta"], 2)
        self.assertEqual(result["pricingApiHitsBefore"], 0)
        self.assertGreaterEqual(result["pricingApiHitsAfter"], 2)
        self.assertGreaterEqual(result["pricingApiHitsDelta"], 2)

    def test_probe_handles_evaluate_failure_gracefully(self) -> None:
        class _RaisingPage(_FakePage):
            def evaluate(self, script):
                raise RuntimeError("boom")

        page = _RaisingPage()
        result = trigger_configurator_via_semantic_labels(
            page=page,
            request_urls=["https://example.test/foo"],
        )

        self.assertTrue(result["attempted"])
        self.assertIn("error", result)
        self.assertEqual(result["requestCountDelta"], 0)
        self.assertEqual(result["clicksPerformed"], [])

    def test_probe_noop_when_no_groups_or_no_clicks_allowed(self) -> None:
        page = _FakePage(evaluate_values=[{"groupsDetected": [], "clicksPerformed": []}])

        result_empty = trigger_configurator_via_semantic_labels(
            page=page,
            request_urls=[],
            keyword_groups={},
        )
        self.assertEqual(result_empty["reason"], "noop")

        result_zero = trigger_configurator_via_semantic_labels(
            page=page,
            request_urls=[],
            max_clicks=0,
        )
        self.assertEqual(result_zero["reason"], "noop")

    def test_probe_script_contains_default_german_keywords(self) -> None:
        # Verify that the script handed to the browser actually carries the German
        # configurator vocabulary. This locks down the website-agnostic intent: the
        # mechanism is generic, but the default vocabulary is German printing.
        page = _FakePage(evaluate_values=[{"groupsDetected": [], "clicksPerformed": []}])
        trigger_configurator_via_semantic_labels(page=page, request_urls=[])

        self.assertEqual(len(page.evaluate_scripts), 1)
        script = page.evaluate_scripts[0]
        for keyword in ["Menge", "Auflage", "Papier", "Format", "Größe", "Bindung"]:
            self.assertIn(keyword, script, f"missing default German keyword: {keyword}")

    def test_probe_accepts_custom_keyword_groups(self) -> None:
        page = _FakePage(evaluate_values=[{"groupsDetected": [], "clicksPerformed": []}])
        custom = {"quantity": ["Quantity", "Count"]}
        trigger_configurator_via_semantic_labels(
            page=page,
            request_urls=[],
            keyword_groups=custom,
        )

        script = page.evaluate_scripts[0]
        self.assertIn("Quantity", script)
        # Default German vocabulary should NOT leak through when custom groups are passed.
        self.assertNotIn("Auflage", script)

    def test_default_german_keywords_cover_core_printing_domain(self) -> None:
        # Smoke test on the default vocabulary so future edits don't drop critical terms.
        for required_group in ("menge", "papier", "format", "farbigkeit"):
            self.assertIn(required_group, DEFAULT_GERMAN_PRINTING_KEYWORDS)
            self.assertTrue(DEFAULT_GERMAN_PRINTING_KEYWORDS[required_group])


if __name__ == "__main__":
    unittest.main()
