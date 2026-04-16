from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from .option_aliases import build_option_value_aliases
from .request_templates import (
  extract_option_catalog_and_templates,
  set_dropdown_maps,
  set_request_template_debug,
)
from .saxoprint_trace_parsing import (
  find_saxoprint_get_product_values_body_from_traces,
  find_saxoprint_portalcode_from_traces,
  find_saxoprint_product_group_id_from_traces,
)


@dataclass
class BootstrapResult:
    site_name: str
    observed_requests: list[str] = field(default_factory=list)
    network_traces: list[dict[str, Any]] = field(default_factory=list)
    option_groups: list[dict[str, Any]] = field(default_factory=list)
    option_catalog: list[dict[str, Any]] = field(default_factory=list)
    option_dependencies: list[dict[str, Any]] = field(default_factory=list)
    dependency_probe: dict[str, Any] = field(default_factory=dict)
    option_application: dict[str, Any] = field(default_factory=dict)
    request_templates: dict[str, Any] = field(default_factory=dict)
    quantity_signal: dict[str, Any] = field(default_factory=dict)
    dom_price_candidates_before: list[float] = field(default_factory=list)
    dom_price_candidates_after: list[float] = field(default_factory=list)
    dom_price_candidates: list[float] = field(default_factory=list)
    selected_configuration: dict[str, Any] = field(default_factory=dict)
    selected_configuration_rows: list[dict[str, Any]] = field(default_factory=list)
    option_url_variants: list[dict[str, Any]] = field(default_factory=list)
    price_summary_lines: list[str] = field(default_factory=list)
    consent_state: dict[str, Any] = field(default_factory=dict)
    token_indicators: list[str] = field(default_factory=list)
    cookie_names: list[str] = field(default_factory=list)
    cookies: dict[str, str] = field(default_factory=dict)
    requires_session: bool = False
    anti_bot_suspected: bool = False
    warnings: list[str] = field(default_factory=list)


def infer_site_name(product_url: str) -> str:
    host = urlparse(product_url).netloc.lower().strip()
    if host.startswith("www."):
        host = host[4:]
    return host.split(":")[0] or "unknown-site"


def bootstrap_product_url(
    product_url: str,
    headless: bool = True,
    timeout_ms: int = 20000,
    max_observed_requests: int = 250,
    dependency_probe_steps: int = 6,
    requested_options: dict[str, Any] | None = None,
    auto_accept_cookies: bool = True,
    debug_hold_seconds: float = 0.0,
    variant_callback_depth: int = 0,
    visited_variant_urls: set[str] | None = None,
) -> BootstrapResult:
    result = BootstrapResult(site_name=infer_site_name(product_url))

    try:
        sync_api = importlib.import_module("playwright.sync_api")
        PlaywrightTimeoutError = getattr(sync_api, "TimeoutError")
        sync_playwright = getattr(sync_api, "sync_playwright")
    except Exception:
        result.warnings.append(
            "Playwright is not available. Install dependencies and run playwright install."
        )
        return result

    request_urls: list[str] = []
    network_traces: list[dict[str, Any]] = []
    token_indicators: set[str] = set()
    saw_waf_status = False
    variant_url_signals: list[dict[str, Any]] = []
    variant_callback_target: dict[str, Any] | None = None
    visited_urls = set(visited_variant_urls or set())

    with sync_playwright() as p:
      browser = p.chromium.launch(headless=headless)
      context = browser.new_context()
      page = context.new_page()

      def _count_configurator_api_requests() -> int:
        hits = 0
        for url in request_urls:
          lowered = url.lower()
          if ("/api/" not in lowered) and ("://api." not in lowered):
            continue
          if any(token in lowered for token in ("itemmaster", "shopping-cart", "calculation", "product")):
            hits += 1
        return hits

      def _wait_for_configurator_readiness() -> dict[str, Any]:
        max_wait_ms = max(2000, min(12000, timeout_ms // 2))
        waited_ms = 0
        poll_ms = 350
        reason = "timeout"
        interactive_count = 0

        while waited_ms <= max_wait_ms:
          api_hits = _count_configurator_api_requests()
          try:
            interactive_count = int(
              page.evaluate(
                r"""
                () => {
                  const selectors = [
                  'input[type="radio"]',
                  'input[type="checkbox"]',
                  'select',
                  '[role="option"]',
                  '[data-option-id]',
                  '[data-property-id]',
                  ];
                  const count = selectors
                  .map((s) => document.querySelectorAll(s).length)
                  .reduce((a, b) => a + b, 0);
                  return Number.isFinite(count) ? count : 0;
                }
                """
              )
            )
          except Exception:
            interactive_count = 0

          if interactive_count >= 2:
            reason = "interactive_controls"
            break
          if api_hits >= 2:
            reason = "pricing_api_activity"
            break

          try:
            page.wait_for_timeout(poll_ms)
          except Exception:
            break
          waited_ms += poll_ms

        return {
          "waitedMs": waited_ms,
          "reason": reason,
          "interactiveCount": interactive_count,
          "apiHits": _count_configurator_api_requests(),
        }

      def _attempt_cookie_consent() -> dict[str, Any]:
        state: dict[str, Any] = {
          "attempted": False,
          "bannerDetected": False,
          "clicked": False,
          "matchedSelector": None,
          "matchedText": None,
          "frameUrl": None,
        }
        if not auto_accept_cookies:
          return state

        state["attempted"] = True
        selector_candidates = [
          "button#onetrust-accept-btn-handler",
          "button[aria-label*='accept' i]",
          "button[title*='accept' i]",
          "button[data-testid*='accept' i]",
          "button[class*='accept' i]",
          "button[id*='accept' i]",
          "[role='button'][aria-label*='accept' i]",
          "button[aria-label*='zustimm' i]",
          "button[title*='zustimm' i]",
          "button[class*='zustimm' i]",
          "button[id*='zustimm' i]",
        ]
        text_patterns = [
          "accept all",
          "accept",
          "allow all",
          "agree",
          "i agree",
          "alle akzeptieren",
          "akzeptieren",
          "zustimmen",
          "einverstanden",
          "alle zulassen",
        ]

        frames = list(page.frames)
        for frame in frames:
          for selector in selector_candidates:
            try:
              handle = frame.query_selector(selector)
              if handle is None:
                continue
              state["bannerDetected"] = True
              if handle.is_visible():
                handle.click(timeout=1000)
                state["clicked"] = True
                state["matchedSelector"] = selector
                state["frameUrl"] = frame.url
                return state
            except Exception:
              continue

          try:
            elements = frame.query_selector_all("button, [role='button'], input[type='button'], input[type='submit'], a")
          except Exception:
            elements = []

          for el in elements[:300]:
            try:
              text = (el.inner_text() or "").strip().lower()
            except Exception:
              text = ""
            if not text:
              try:
                text = str(el.get_attribute("value") or "").strip().lower()
              except Exception:
                text = ""
            if not text:
              continue

            if any(pattern in text for pattern in text_patterns):
              state["bannerDetected"] = True
              try:
                el.click(timeout=1000)
                state["clicked"] = True
                state["matchedText"] = text[:120]
                state["frameUrl"] = frame.url
                return state
              except Exception:
                continue

        return state

      def on_request(req) -> None:
          if len(request_urls) >= max_observed_requests:
              return
          request_urls.append(req.url)
          headers = {k.lower(): str(v) for k, v in req.headers.items()}
          if "x-csrf-token" in headers or "csrf" in " ".join(headers.keys()):
              token_indicators.add("csrf")
          if "authorization" in headers and "bearer" in (headers.get("authorization", "") or "").lower():
              token_indicators.add("bearer")

      def on_response(res) -> None:
          nonlocal saw_waf_status
          if res.status in (403, 429):
              saw_waf_status = True

          headers = {k.lower(): str(v) for k, v in res.headers.items()}
          set_cookie = headers.get("set-cookie", "")
          if set_cookie:
              result.requires_session = True
          if "cf-ray" in headers or "x-akamai" in headers or "x-sucuri" in headers:
              saw_waf_status = True

          if len(network_traces) >= max_observed_requests:
              return

          req = res.request
          req_headers = {k.lower(): str(v) for k, v in req.headers.items()}
          try:
            post_data = req.post_data
          except Exception:
            post_data = None

          post_data_limit = 8000
          try:
            if infer_site_name(req.url) == result.site_name:
              post_data_limit = 60000
          except Exception:
            post_data_limit = 8000

          post_data_len: int | None = None
          post_data_truncated: bool | None = None
          post_data_captured: str | None = None
          post_data_preview: str | None = None
          if isinstance(post_data, str) and post_data:
            post_data_len = len(post_data)
            post_data_truncated = post_data_len > post_data_limit
            post_data_captured = post_data[:post_data_limit]
            post_data_preview = post_data[:500]
          response_body_preview = None

          should_capture_preview = (
            req.resource_type in {"fetch", "xhr"}
            or "json" in (headers.get("content-type", "") or "").lower()
            or "/api" in req.url.lower()
          )
          if should_capture_preview:
            try:
              body_text = res.text()
              if body_text:
                preview_limit = 16000
                url_lower = req.url.lower()
                if "get-product-values" in url_lower:
                  preview_limit = 80000
                response_body_preview = body_text[:preview_limit]
            except Exception:
              response_body_preview = None

          request_headers = {
              k: req_headers[k]
              for k in [
                "content-type",
                "accept",
                "portal",
                "portalcode",
                "portal-code",
                "x-csrf-token",
                "authorization",
                "x-requested-with",
              ]
              if k in req_headers
          }
          response_headers = {
              k: headers[k]
              for k in ["content-type", "set-cookie", "cache-control", "etag"]
              if k in headers
          }
          network_traces.append(
              {
                  "url": req.url,
                  "method": req.method,
                  "resource_type": req.resource_type,
                  "status": int(res.status),
                  "request_content_type": req_headers.get("content-type"),
                  "response_content_type": headers.get("content-type"),
                  "request_headers": request_headers,
                  "response_headers": response_headers,
                "post_data": post_data_captured,
                "post_data_preview": post_data_preview,
                "post_data_len": post_data_len,
                "post_data_truncated": post_data_truncated,
                  "response_body_preview": response_body_preview,
              }
          )

      page.on("request", on_request)
      page.on("response", on_response)

      try:
          page.goto(product_url, wait_until="domcontentloaded", timeout=timeout_ms)
          page.wait_for_timeout(1500)
          try:
            page.wait_for_load_state("networkidle", timeout=min(5000, timeout_ms))
          except Exception:
            pass
      except PlaywrightTimeoutError:
          result.warnings.append("Navigation timed out; partial network observations were collected.")
      except Exception as exc:
          result.warnings.append(f"Navigation error: {exc}")

      try:
        result.consent_state = _attempt_cookie_consent()
        if result.consent_state.get("clicked"):
          page.wait_for_timeout(500)
        try:
          page.wait_for_load_state("networkidle", timeout=min(3500, timeout_ms))
        except Exception:
          pass
      except Exception as exc:
        result.warnings.append(f"Consent handling error: {exc}")

      try:
        readiness = _wait_for_configurator_readiness()
        result.consent_state["readiness"] = readiness
        if (
          readiness.get("reason") == "timeout"
          and int(readiness.get("interactiveCount") or 0) == 0
          and int(readiness.get("apiHits") or 0) == 0
        ):
          result.warnings.append(
            "Configurator did not become ready before DOM probe; captured mostly static assets."
          )
      except Exception as exc:
        result.warnings.append(f"Readiness wait error: {exc}")

      content = ""
      try:
          content = page.content().lower()
      except Exception:
          pass

      requested_options_raw = dict(requested_options or {})
      option_value_aliases = build_option_value_aliases(result.site_name)
      options_for_dom_apply = dict(requested_options_raw)
      pre_applied_format_matches: list[dict[str, Any]] = []
      pre_applied_format_unmatched: list[dict[str, Any]] = []
      pre_applied_format_unmatched_keys: set[str] = set()

      target_slug = urlparse(product_url).path.lower().split("/p/")[-1].split("?")[0]
      target_family = target_slug
      for marker in ["-din-", "-querformat", "-quadratisch"]:
        if marker in target_family:
          target_family = target_family.split(marker, 1)[0]
      product_family_tokens = [token for token in target_family.split("-") if len(token) >= 3][:6]

      def _normalize_loose(value: Any) -> str:
        text = str(value or "").strip().lower()
        if not text:
          return ""
        text = text.replace("&", " and ").replace("²", "2")
        cleaned = "".join(ch if ch.isalnum() else " " for ch in text)
        return " ".join(cleaned.split())

      def _key_aliases(raw_key: Any) -> list[str]:
        key = _normalize_loose(raw_key)
        aliases = {key}
        if any(token in key for token in ["format", "size", "endformat", "din", "groesse", "grosse"]):
          aliases.update(["format", "endformat", "size", "din", "groesse", "grosse"])
        return [v for v in aliases if v]

      def _expand_value_variants(raw_value: Any) -> list[str]:
        loose = _normalize_loose(raw_value)
        variants: set[str] = set()
        if loose:
          variants.add(loose)

        compact = loose.replace(" ", "")
        for alias in option_value_aliases.get(compact, []):
          normalized = _normalize_loose(alias)
          if normalized:
            variants.add(normalized)

        if loose.startswith("din "):
          variants.add(loose.replace("din ", "", 1).strip())
        if len(loose) == 2 and loose[0] in {"a", "b"} and loose[1].isdigit():
          variants.add(f"din {loose}")

        return [v for v in variants if v]

      baseline_request_count = len(request_urls)

      try:
        for raw_key, raw_value in requested_options_raw.items():
          key_aliases = _key_aliases(raw_key)
          if not any(token in key_aliases for token in ["format", "size", "endformat", "din", "groesse", "grosse"]):
            continue

          desired_variants = _expand_value_variants(raw_value)
          if not desired_variants:
            continue

          # Onlineprinters exposes format variants as dedicated product URLs.
          if result.site_name == "onlineprinters.de":
            try:
              variant_rows = page.evaluate(
                r"""
                () => {
                  const normalize = (v) => (v || '').toString().trim();
                  const rows = Array.from(document.querySelectorAll('a[href*="/p/"]')).slice(0, 600);
                  return rows.map((el) => ({
                    text: normalize(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || ''),
                    href: normalize(el.getAttribute('href') || ''),
                  }));
                }
                """
              )
            except Exception:
              variant_rows = []

            best_variant: dict[str, Any] | None = None
            best_variant_score = -1
            for row in variant_rows:
              href_raw = str(row.get("href") or "").strip()
              if not href_raw:
                continue
              href_url = urljoin(page.url, href_raw)
              href = _normalize_loose(href_url)
              text = _normalize_loose(row.get("text"))
              combined = " ".join(v for v in [href, text] if v)
              combined_compact = combined.replace(" ", "")
              if not combined:
                continue

              family_hits = 0
              if product_family_tokens:
                family_hits = sum(1 for token in product_family_tokens if token in href)
                if family_hits == 0:
                  continue

              score = min(16, family_hits * 4)
              combined_tokens = set(combined.split())
              for desired in desired_variants:
                desired_compact = desired.replace(" ", "")
                desired_tokens = set(desired.split())
                if desired_compact and desired_compact in combined_compact:
                  score += 18
                if desired in combined:
                  score += 8
                overlap = len(combined_tokens.intersection(desired_tokens))
                score += min(8, overlap * 2)

              if score > best_variant_score:
                best_variant_score = score
                best_variant = {
                  "href": href_url,
                  "text": str(row.get("text") or ""),
                }

            if best_variant is not None and best_variant_score >= 20:
              target_url = str(best_variant.get("href") or "").strip()
              variant_callback_target = {
                "key": str(raw_key),
                "value": raw_value,
                "targetUrl": target_url,
                "label": str(best_variant.get("text") or ""),
                "score": int(best_variant_score),
              }
              variant_url_signals.append(
                {
                  "key": str(raw_key),
                  "value": raw_value,
                  "status": "resolved_variant_url",
                  "targetUrl": target_url,
                  "label": str(best_variant.get("text") or ""),
                  "score": int(best_variant_score),
                  "mode": "callback",
                }
              )
              break

            pre_applied_format_unmatched.append(
              {
                "key": str(raw_key),
                "value": raw_value,
                "reason": "variant_url_not_found",
              }
            )
            variant_url_signals.append(
              {
                "key": str(raw_key),
                "value": raw_value,
                "status": "variant_url_not_found",
              }
            )
            pre_applied_format_unmatched_keys.add(str(raw_key).strip().lower())
            continue

          link_selector = 'a[data-option-id], a[data-property-id], a[href*="depvar_index_setparent"], a[href*="otpmoreformats"], a[class*="format" i]'
          try:
            link_rows = page.evaluate(
              r"""
              (selector) => {
                const normalize = (v) => (v || '').toString().trim();
                const rows = Array.from(document.querySelectorAll(selector)).slice(0, 500);
                return rows.map((el, idx) => {
                  const groupNode = el.closest('[data-property], [data-option-group], [class*="option"], [class*="property"], li, ul, div');
                  return {
                    idx,
                    text: normalize(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || ''),
                    href: normalize(el.getAttribute('href') || ''),
                    group: normalize(groupNode ? groupNode.getAttribute('data-property-name') || groupNode.getAttribute('data-option-group') || groupNode.getAttribute('class') || '' : ''),
                  };
                });
              }
              """,
              link_selector,
            )
          except Exception:
            link_rows = []

          best_row: dict[str, Any] | None = None
          best_score = -1
          for row in link_rows:
            text = _normalize_loose(row.get("text"))
            href = _normalize_loose(row.get("href"))
            group = _normalize_loose(row.get("group"))
            combined = " ".join(v for v in [text, href, group] if v)
            combined_compact = combined.replace(" ", "")
            if not combined:
              continue

            score = 0
            if any(alias in combined for alias in key_aliases):
              score += 4
            if "depvar index setparent" in href or "otpmoreformats" in href:
              score += 3
            if href and product_family_tokens:
              family_hits = sum(1 for token in product_family_tokens if token in href)
              if family_hits > 0:
                score += min(12, family_hits * 4)
              else:
                score -= 12

            combined_tokens = set(combined.split())
            for desired in desired_variants:
              desired_compact = desired.replace(" ", "")
              desired_tokens = set(desired.split())
              if combined == desired:
                score += 16
              elif desired in combined or combined in desired:
                score += 9
              if desired_compact and desired_compact in combined_compact:
                score += 6
              overlap = len(combined_tokens.intersection(desired_tokens))
              score += min(8, overlap * 2)

            if score > best_score:
              best_score = score
              best_row = row

          if best_row is None or best_score < 6:
            continue

          try:
            clicked = False
            click_mode = "pre_navigation"
            try:
              page.locator(link_selector).nth(int(best_row.get("idx", 0))).click(timeout=min(3500, timeout_ms))
              clicked = True
            except Exception:
              clicked = False

            if not clicked:
              clicked = bool(
                page.evaluate(
                  r"""
                  (href, text) => {
                    const normalize = (v) => (v || '').toString().trim().toLowerCase();
                    const links = Array.from(document.querySelectorAll('a'));
                    let target = null;

                    const hrefNorm = normalize(href);
                    if (hrefNorm) {
                      target = links.find((el) => normalize(el.getAttribute('href')) === hrefNorm) || null;
                      if (!target) {
                        target = links.find((el) => {
                          const h = normalize(el.getAttribute('href'));
                          return !!h && (h.includes(hrefNorm) || hrefNorm.includes(h));
                        }) || null;
                      }
                    }

                    if (!target) {
                      const textNorm = normalize(text);
                      if (textNorm) {
                        target = links.find((el) => {
                          const t = normalize(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '');
                          return !!t && (t.includes(textNorm) || textNorm.includes(t));
                        }) || null;
                      }
                    }

                    if (!target) return false;
                    target.click();
                    return true;
                  }
                  """,
                  str(best_row.get("href") or ""),
                  str(best_row.get("text") or ""),
                )
              )
              if clicked:
                click_mode = "pre_navigation_eval"

            if not clicked:
              raise RuntimeError("unable_to_click_format_link")

            try:
              page.wait_for_load_state("domcontentloaded", timeout=min(6000, timeout_ms))
            except Exception:
              pass
            try:
              page.wait_for_load_state("networkidle", timeout=min(4500, timeout_ms))
            except Exception:
              pass
            page.wait_for_timeout(350)
            pre_applied_format_matches.append(
              {
                "key": str(raw_key),
                "value": raw_value,
                "group": str(best_row.get("group") or ""),
                "controlTag": "a",
                "controlLabel": str(best_row.get("text") or best_row.get("href") or ""),
                "score": int(best_score),
                "pass": click_mode,
              }
            )
            options_for_dom_apply.pop(str(raw_key), None)
          except Exception:
            pre_applied_format_unmatched.append(
              {
                "key": str(raw_key),
                "value": raw_value,
                "reason": "link_click_failed",
                "candidateScore": int(best_score),
                "candidateText": str(best_row.get("text") or ""),
                "candidateHref": str(best_row.get("href") or ""),
              }
            )
            pre_applied_format_unmatched_keys.add(str(raw_key).strip().lower())
      except Exception:
        # Pre-navigation option application is best-effort.
        pass

      if variant_callback_target is None:
        try:
            dom_signals = page.evaluate(
            r"""
              async (args) => {
                const normalize = (v) => (v || '').toString().trim();
                const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
                const probeSteps = Math.max(0, Math.min(12, Number(args?.dependencyProbeSteps || 0)));

                const cleanupGroupLabel = (value) => {
                  let v = normalize(value)
                    .replace(/_essential$/i, '')
                    .replace(/^input_var_/i, '')
                    .replace(/^inputvar_/i, '')
                    .replace(/[_-]+/g, ' ')
                    .replace(/\s+/g, ' ')
                    .trim();
                  if (!v) return '';
                  // Keep natural-language labels over internal technical IDs.
                  if (/^(?:[A-Z]{3,}\d{2,}|Z?[A-Z]{2,}\d{2,}[A-Z0-9_]*)$/.test(v)) {
                    return '';
                  }
                  return v;
                };

                const getAssociatedLabelText = (el) => {
                  const id = normalize(el.getAttribute('id'));
                  if (id) {
                    const linked = document.querySelector(`label[for="${id.replace(/"/g, '\\"')}"]`);
                    if (linked && normalize(linked.textContent)) return normalize(linked.textContent);
                  }

                  const ariaLabel = normalize(el.getAttribute('aria-label'));
                  if (ariaLabel) return ariaLabel;

                  const labelledBy = normalize(el.getAttribute('aria-labelledby'));
                  if (labelledBy) {
                    const parts = labelledBy.split(/\s+/).filter(Boolean);
                    const joined = parts
                      .map((pid) => {
                        const node = document.getElementById(pid);
                        return normalize(node ? node.textContent : '');
                      })
                      .filter(Boolean)
                      .join(' ')
                      .trim();
                    if (joined) return joined;
                  }

                  const parentLabel = el.closest('label');
                  if (parentLabel && normalize(parentLabel.textContent)) {
                    return normalize(parentLabel.textContent);
                  }

                  return '';
                };

                const findGroupLabel = (el) => {
                  const associated = cleanupGroupLabel(getAssociatedLabelText(el));
                  if (associated) return associated;

                  const attrLabel = normalize(el.getAttribute('data-property-name') || el.getAttribute('data-label'));
                  const cleanedAttrLabel = cleanupGroupLabel(attrLabel);
                  if (cleanedAttrLabel) return cleanedAttrLabel;

                  const fieldset = el.closest('fieldset');
                  if (fieldset) {
                    const legend = fieldset.querySelector('legend');
                    const cleanedLegend = cleanupGroupLabel(legend ? legend.textContent : '');
                    if (cleanedLegend) return cleanedLegend;
                  }

                  const section = el.closest('[data-property], [data-option-group], [class*="option"], [class*="property"]');
                  if (section) {
                    const heading = section.querySelector('h1, h2, h3, h4, h5, label');
                    const cleanedHeading = cleanupGroupLabel(heading ? heading.textContent : '');
                    if (cleanedHeading) return cleanedHeading;

                    const semanticHint = cleanupGroupLabel(
                      section.getAttribute('data-property-name') ||
                      section.getAttribute('data-option-group') ||
                      section.getAttribute('data-name') ||
                      section.getAttribute('data-testid')
                    );
                    if (semanticHint) return semanticHint;
                  }

                  const fallback = cleanupGroupLabel(el.getAttribute('name') || el.getAttribute('id') || '');
                  return fallback || 'unknown';
                };

                const map = new Map();
                const controls = document.querySelectorAll('input[type="radio"], input[type="checkbox"], select, button[aria-pressed], [role="option"], [data-option-id], [data-property-id], a[data-option-id], a[data-property-id], a[href*="depvar_index_setparent"], a[href*="otpmoreformats"], a[class*="format" i]');
                controls.forEach((el) => {
                  const group = findGroupLabel(el);
                  if (!map.has(group)) {
                    map.set(group, {
                      group,
                      control_types: new Set(),
                      values: new Set(),
                    });
                  }
                  const entry = map.get(group);
                  const tag = el.tagName.toLowerCase();
                  const type = normalize(el.getAttribute('type')) || tag;
                  entry.control_types.add(type);

                  if (tag === 'select') {
                    Array.from(el.querySelectorAll('option')).forEach((opt) => {
                      const label = normalize(opt.textContent);
                      if (label) entry.values.add(label);
                    });
                  } else {
                    const label = normalize(el.getAttribute('value') || el.getAttribute('data-value') || el.textContent);
                    if (label) entry.values.add(label);
                  }
                });

                const optionGroups = Array.from(map.values()).map((entry) => ({
                  group: entry.group,
                  controlTypes: Array.from(entry.control_types),
                  optionCount: entry.values.size,
                  sampleValues: Array.from(entry.values).slice(0, 8),
                }));

                const groupNames = optionGroups.map((g) => g.group.toLowerCase()).filter(Boolean);
                const dependencyAttrs = [
                  'data-parent',
                  'data-depends-on',
                  'data-dependent-on',
                  'data-controls',
                  'aria-controls',
                  'data-filter-by',
                  'data-source-property',
                ];
                const depMap = new Map();
                const addEdge = (source, target, signal) => {
                  const s = normalize(source);
                  const t = normalize(target);
                  if (!s || !t || s === t) return;
                  const key = `${s}=>${t}`;
                  if (!depMap.has(key)) {
                    depMap.set(key, { source: s, target: t, signals: new Set() });
                  }
                  depMap.get(key).signals.add(signal);
                };

                const allControls = document.querySelectorAll('input, select, button, [role="option"], [data-option-id], [data-property-id], a[data-option-id], a[data-property-id], a[href*="depvar_index_setparent"], a[href*="otpmoreformats"], a[class*="format" i]');
                allControls.forEach((el) => {
                  const sourceGroup = findGroupLabel(el);
                  dependencyAttrs.forEach((attr) => {
                    const raw = normalize(el.getAttribute(attr));
                    if (!raw) return;
                    const lower = raw.toLowerCase();
                    const target = groupNames.find((name) => lower.includes(name));
                    if (target) addEdge(sourceGroup, target, attr);
                  });
                });

                const optionDependencies = Array.from(depMap.values()).map((edge) => ({
                  source: edge.source,
                  target: edge.target,
                  strength: edge.signals.size,
                  signals: Array.from(edge.signals),
                }));

                const interactiveSelector = [
                  'input[type="radio"]',
                  'input[type="checkbox"]',
                  'select',
                  '[role="option"]',
                  '[data-option-id]',
                  '[data-property-id]',
                  'button[aria-pressed]',
                  'a[data-option-id]',
                  'a[data-property-id]',
                  'a[href*="depvar_index_setparent"]',
                  'a[href*="otpmoreformats"]',
                  'a[class*="format" i]',
                ].join(', ');
                const allInteractiveControls = Array.from(document.querySelectorAll(interactiveSelector));
                const isEnabled = (el) => !el.disabled && el.getAttribute('aria-disabled') !== 'true';

                const snapshotGroupState = () => {
                  const state = new Map();
                  const controls = Array.from(document.querySelectorAll(interactiveSelector));
                  controls.forEach((el) => {
                    const group = findGroupLabel(el);
                    if (!state.has(group)) {
                      state.set(group, { total: 0, enabled: 0 });
                    }
                    const rec = state.get(group);
                    rec.total += 1;
                    if (isEnabled(el)) rec.enabled += 1;
                  });
                  return state;
                };

                const probeCandidates = allInteractiveControls.filter((el) => {
                  if (el.tagName.toLowerCase() === 'a') return false;
                  if (!isEnabled(el)) return false;
                  const group = findGroupLabel(el);
                  return !!group && group !== 'unknown';
                }).slice(0, probeSteps);

                const activeProbeEdges = new Map();
                const addActiveProbeEdge = (source, target) => {
                  const s = normalize(source);
                  const t = normalize(target);
                  if (!s || !t || s === t) return;
                  const key = `${s}=>${t}`;
                  if (!activeProbeEdges.has(key)) {
                    activeProbeEdges.set(key, { source: s, target: t, changedMetrics: 0 });
                  }
                  activeProbeEdges.get(key).changedMetrics += 1;
                  addEdge(s, t, 'active_probe');
                };

                const interact = async (el) => {
                  const tag = el.tagName.toLowerCase();
                  if (tag === 'select') {
                    const options = Array.from(el.options || []).filter((o) => !o.disabled);
                    if (options.length > 1) {
                      const current = el.selectedIndex;
                      const nextIdx = options.findIndex((o) => o.index !== current);
                      if (nextIdx >= 0) {
                        el.value = options[nextIdx].value;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                      }
                    }
                  } else if (tag === 'input') {
                    const type = normalize(el.getAttribute('type')).toLowerCase();
                    if (type === 'radio' || type === 'checkbox') {
                      el.click();
                    }
                  } else {
                    el.click();
                  }
                  await wait(250);
                };

                for (const el of probeCandidates) {
                  const sourceGroup = findGroupLabel(el);
                  const before = snapshotGroupState();
                  try {
                    await interact(el);
                  } catch {
                    continue;
                  }
                  const after = snapshotGroupState();

                  after.forEach((afterRec, group) => {
                    if (group === sourceGroup) return;
                    const beforeRec = before.get(group) || { total: 0, enabled: 0 };
                    if (afterRec.total !== beforeRec.total || afterRec.enabled !== beforeRec.enabled) {
                      addActiveProbeEdge(sourceGroup, group);
                    }
                  });
                }

                const activeProbeDependencies = Array.from(activeProbeEdges.values());

                const quantityRegex = /(quantity|auflage|qty|amount|menge)/i;
                const quantityInputs = Array.from(document.querySelectorAll('input')).filter((el) => {
                  const bag = [el.name, el.id, el.placeholder, el.className].join(' ');
                  return quantityRegex.test(bag);
                });

                const quantityPresetCandidates = Array.from(document.querySelectorAll('button, a, li, option, [data-quantity], [data-amount]'));
                const quantityPresets = new Set();
                quantityPresetCandidates.forEach((el) => {
                  const context = [
                    el.getAttribute('data-quantity'),
                    el.getAttribute('data-amount'),
                    el.getAttribute('class'),
                    el.getAttribute('id'),
                    el.getAttribute('name'),
                    el.textContent,
                  ].join(' ');
                  if (!quantityRegex.test(context)) return;
                  const text = normalize(el.getAttribute('data-quantity') || el.getAttribute('data-amount') || el.textContent);
                  const match = text.match(/\\b\\d{2,6}\\b/);
                  if (match) {
                    const n = Number(match[0]);
                    if (Number.isFinite(n) && n > 0 && n <= 1000000) quantityPresets.add(n);
                  }
                });

                let mode = 'unknown';
                if (quantityInputs.length && quantityPresets.size) mode = 'mixed';
                else if (quantityInputs.length) mode = 'manual_only';
                else if (quantityPresets.size) mode = 'preset_only';

                const sortedPresets = Array.from(quantityPresets).sort((a, b) => a - b);
                const maxPreset = sortedPresets.length ? sortedPresets[sortedPresets.length - 1] : null;

                const parsePriceFromText = (text) => {
                  const raw = normalize(text).replace(/\u00a0/g, ' ');
                  if (!raw) return null;
                  const match = raw.match(/\d{1,3}(?:[\.\s]\d{3})*(?:[,\.]\d{2})|\d+(?:[,\.]\d{2})/);
                  if (!match) return null;
                  let value = match[0].replace(/\s+/g, '');
                  const commaIdx = value.lastIndexOf(',');
                  const dotIdx = value.lastIndexOf('.');
                  if (commaIdx >= 0 && dotIdx >= 0) {
                    if (commaIdx > dotIdx) {
                      value = value.replace(/\./g, '').replace(',', '.');
                    } else {
                      value = value.replace(/,/g, '');
                    }
                  } else if (commaIdx >= 0) {
                    value = value.replace(/\./g, '').replace(',', '.');
                  } else {
                    const parts = value.split('.');
                    if (parts.length > 2) value = parts.join('');
                  }
                  const parsed = Number(value);
                  if (!Number.isFinite(parsed) || parsed <= 0 || parsed > 1000000) return null;
                  return parsed;
                };

                const collectPriceCandidates = () => {
                  const priceSelector = '[data-price], [class*="price"], [id*="price"], [itemprop="price"], [data-testid*="price"]';
                  const priceTexts = [];
                  Array.from(document.querySelectorAll(priceSelector)).forEach((el) => {
                    const text = normalize(el.getAttribute('content') || el.getAttribute('data-price') || el.textContent);
                    if (text) priceTexts.push(text);
                  });

                  if (!priceTexts.length) {
                    const bodyText = normalize(document.body ? document.body.innerText : '');
                    const snippets = bodyText.split(/\n+/).slice(0, 400);
                    snippets.forEach((line) => {
                      if (/€|eur|price|preis/i.test(line)) priceTexts.push(line);
                    });
                  }

                  const priceCandidates = [];
                  const seenPrices = new Set();
                  priceTexts.forEach((text) => {
                    const p = parsePriceFromText(text);
                    if (p === null) return;
                    const k = p.toFixed(2);
                    if (seenPrices.has(k)) return;
                    seenPrices.add(k);
                    priceCandidates.push(p);
                  });
                  return priceCandidates;
                };

                const requestedOptions = (args && args.requestedOptions && typeof args.requestedOptions === 'object')
                  ? args.requestedOptions
                  : {};
                const optionValueAliases = (args && args.optionValueAliases && typeof args.optionValueAliases === 'object')
                  ? args.optionValueAliases
                  : {};
                const normalizeLoose = (v) => normalize(v)
                  .toLowerCase()
                  .normalize('NFKD')
                  .replace(/[\u0300-\u036f]/g, '')
                  .replace(/&/g, ' and ')
                  .replace(/[^a-z0-9]+/g, ' ')
                  .replace(/\s+/g, ' ')
                  .trim();
                const normalizeCompact = (v) => normalizeLoose(v).replace(/\s+/g, '');
                const expandValueVariants = (rawValue) => {
                  const loose = normalizeLoose(rawValue);
                  const compact = normalizeCompact(rawValue);
                  const variants = new Set();
                  if (loose) variants.add(loose);

                  const aliasList = optionValueAliases[compact] || [];
                  aliasList.forEach((alias) => {
                    const v = normalizeLoose(alias);
                    if (v) variants.add(v);
                  });

                  // Common print-value normalization fallback.
                  const grammage = loose.match(/^(\d{2,4})\s*g(?:sm|\/m2|\/m\^2|\/m²)?$/i);
                  if (grammage) {
                    const g = grammage[1];
                    variants.add(`${g} g`);
                    variants.add(`${g} g/m2`);
                    variants.add(`${g} g/m²`);
                  }

                  const size = loose.match(/^(?:din\s*)?([ab]\s*\d)$/i);
                  if (size) {
                    const s = size[1].replace(/\s+/g, '').toUpperCase();
                    variants.add(s.toLowerCase());
                    variants.add(`din ${s.toLowerCase()}`);
                  }

                  return Array.from(variants);
                };
                const toTokenSet = (v) => new Set(normalizeLoose(v).split(' ').filter(Boolean));
                const tokenOverlap = (aSet, bSet) => {
                  let count = 0;
                  aSet.forEach((v) => {
                    if (bSet.has(v)) count += 1;
                  });
                  return count;
                };
                const keyAliases = (rawKey) => {
                  const key = normalizeLoose(rawKey);
                  const aliases = new Set([key]);
                  if (/(format|size|endformat|din)/i.test(rawKey)) {
                    ['format', 'endformat', 'size', 'din', 'groesse', 'grosse'].forEach((v) => aliases.add(v));
                  }
                  if (/(material|papier|paper|gramm|grammage|g\/?m2|g\/m\^2|bilderdruck|karton)/i.test(rawKey)) {
                    ['material', 'papier', 'paper', 'gramm', 'grammage', 'bilderdruck', 'karton'].forEach((v) => aliases.add(v));
                  }
                  if (/(color|colour|farbe|bedruck|printing|print)/i.test(rawKey)) {
                    ['farbe', 'color', 'colour', 'druck', 'printing', 'print'].forEach((v) => aliases.add(v));
                  }
                  if (/(seiten|pages|umfang|pagecount)/i.test(rawKey)) {
                    ['seiten', 'pages', 'umfang', 'page'].forEach((v) => aliases.add(v));
                  }
                  return Array.from(aliases).filter(Boolean);
                };

                const preInteractionPriceCandidates = collectPriceCandidates();
                const optionApplication = {
                  requestedCount: Object.keys(requestedOptions).length,
                  matched: [],
                  unmatched: [],
                  strictPassMatched: 0,
                  relaxedPassMatched: 0,
                };

                const controlEntries = allInteractiveControls.map((el) => {
                  const tag = el.tagName.toLowerCase();
                  const group = findGroupLabel(el);
                  const href = normalize(el.getAttribute('href') || '');
                  const label = normalize(
                    el.getAttribute('aria-label') ||
                    el.getAttribute('title') ||
                    el.getAttribute('value') ||
                    el.getAttribute('data-value') ||
                    href ||
                    el.textContent
                  );
                  return {
                    el,
                    tag,
                    group,
                    label,
                    href,
                    groupLoose: normalizeLoose(group),
                    labelLoose: normalizeLoose(label),
                    hrefLoose: normalizeLoose(href),
                  };
                });

                const applyControlValue = async (entry, desiredRaw) => {
                  const desiredVariants = expandValueVariants(desiredRaw);
                  if (!desiredVariants.length) return false;

                  if (entry.tag === 'select') {
                    const options = Array.from(entry.el.options || []);
                    if (!options.length) return false;

                    let best = null;
                    let bestScore = -1;
                    options.forEach((opt) => {
                      const txt = normalizeLoose(opt.textContent || opt.value);
                      if (!txt) return;
                      let score = 0;
                      const txtTokens = toTokenSet(txt);
                      desiredVariants.forEach((desired) => {
                        const desiredTokens = toTokenSet(desired);
                        let local = 0;
                        if (txt === desired) local = 12;
                        else if (txt.includes(desired) || desired.includes(txt)) local = 7;
                        local += Math.min(6, tokenOverlap(txtTokens, desiredTokens) * 2);
                        if (local > score) score = local;
                      });
                      if (score > bestScore) {
                        bestScore = score;
                        best = opt;
                      }
                    });
                    if (best && bestScore > 0) {
                      entry.el.value = best.value;
                      entry.el.dispatchEvent(new Event('input', { bubbles: true }));
                      entry.el.dispatchEvent(new Event('change', { bubbles: true }));
                      await wait(250);
                      return true;
                    }
                    return false;
                  }

                  const loweredLabel = entry.labelLoose;
                  const loweredHref = entry.hrefLoose;
                  if (!loweredLabel && !loweredHref) return false;
                  const valueMatch = desiredVariants.some((desired) => (
                    (loweredLabel && (loweredLabel === desired || loweredLabel.includes(desired) || desired.includes(loweredLabel)))
                    || (loweredHref && (loweredHref.includes(desired) || desired.includes(loweredHref)))
                  ));
                  if (!valueMatch) {
                    return false;
                  }
                  try {
                    const t = normalize(entry.el.getAttribute('type')).toLowerCase();
                    if (entry.tag === 'input' && t === 'checkbox' && entry.el.checked) {
                      return true;
                    }
                    entry.el.click();
                    await wait(250);
                    return true;
                  } catch {
                    return false;
                  }
                };

                const scoreCandidates = (rawKey, rawValue, relaxedMode) => {
                  const aliases = keyAliases(rawKey);
                  const desiredVariants = expandValueVariants(rawValue);
                  const rows = [];

                  for (const entry of controlEntries) {
                    if (!isEnabled(entry.el)) continue;
                    let score = 0;

                    const groupLoose = entry.groupLoose;
                    const labelLoose = entry.labelLoose;
                    const hrefLoose = entry.hrefLoose;
                    const groupCompact = normalizeCompact(entry.group);
                    const labelCompact = normalizeCompact(entry.label);
                    const hrefCompact = normalizeCompact(entry.href);

                    let keyHit = false;
                    aliases.forEach((alias) => {
                      if (!alias) return;
                      if (
                        (groupLoose && groupLoose.includes(alias))
                        || (labelLoose && labelLoose.includes(alias))
                        || (hrefLoose && hrefLoose.includes(alias))
                      ) {
                        keyHit = true;
                        score += 4;
                      }
                    });

                    const entryTokens = toTokenSet(`${entry.group} ${entry.label} ${entry.href}`);
                    desiredVariants.forEach((desired) => {
                      const desiredCompact = normalizeCompact(desired);
                      const desiredTokens = toTokenSet(desired);
                      if (desired && labelLoose) {
                        if (labelLoose === desired) score += 18;
                        else if (labelLoose.includes(desired) || desired.includes(labelLoose)) score += 10;
                      }
                      if (desired && hrefLoose) {
                        if (hrefLoose.includes(desired) || desired.includes(hrefLoose)) score += 8;
                      }
                      if (desiredCompact && labelCompact && (labelCompact.includes(desiredCompact) || desiredCompact.includes(labelCompact))) {
                        score += 6;
                      }
                      if (desiredCompact && hrefCompact && (hrefCompact.includes(desiredCompact) || desiredCompact.includes(hrefCompact))) {
                        score += 5;
                      }
                      score += Math.min(10, tokenOverlap(entryTokens, desiredTokens) * 2);
                    });

                    if (!relaxedMode) {
                      if (!keyHit && score < 12) continue;
                      if (keyHit) score += 2;
                    }

                    if (entry.tag === 'select' && keyHit) score += 2;
                    if (entry.tag === 'a' && keyHit) score += 2;
                    if (score > 0) rows.push({ entry, score });
                  }

                  rows.sort((a, b) => b.score - a.score);
                  return rows.slice(0, 10);
                };

                const collectSelectedConfiguration = () => {
                  const byGroup = {};
                  const rows = [];
                  const append = (group, value, controlTag) => {
                    const g = normalize(group) || 'unknown';
                    const v = normalize(value);
                    if (!v) return;
                    rows.push({ group: g, value: v, controlTag: normalize(controlTag) || 'unknown' });
                    if (!Object.prototype.hasOwnProperty.call(byGroup, g)) {
                      byGroup[g] = v;
                      return;
                    }
                    if (Array.isArray(byGroup[g])) {
                      if (!byGroup[g].includes(v)) byGroup[g].push(v);
                      return;
                    }
                    if (byGroup[g] !== v) {
                      byGroup[g] = [byGroup[g], v];
                    }
                  };

                  const selectedControls = Array.from(document.querySelectorAll(
                    'input[type="radio"], input[type="checkbox"], select, [role="option"], [data-option-id], [data-property-id], button[aria-pressed]'
                  ));

                  selectedControls.forEach((el) => {
                    const tag = el.tagName.toLowerCase();
                    const group = findGroupLabel(el);

                    if (tag === 'select') {
                      const selectedOpt = el.options && el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
                      if (selectedOpt) {
                        append(group, selectedOpt.textContent || selectedOpt.value, tag);
                      }
                      return;
                    }

                    if (tag === 'input') {
                      const type = normalize(el.getAttribute('type')).toLowerCase();
                      if ((type === 'radio' || type === 'checkbox') && !el.checked) return;
                    }

                    const ariaSelected = normalize(el.getAttribute('aria-selected')).toLowerCase();
                    const ariaPressed = normalize(el.getAttribute('aria-pressed')).toLowerCase();
                    if (tag !== 'input') {
                      if (ariaSelected && ariaSelected !== 'true' && ariaPressed && ariaPressed !== 'true') {
                        return;
                      }
                    }

                    const value =
                      el.getAttribute('data-label') ||
                      el.getAttribute('aria-label') ||
                      el.getAttribute('title') ||
                      el.getAttribute('value') ||
                      el.getAttribute('data-value') ||
                      el.textContent;
                    append(group, value, tag);
                  });

                  return {
                    selectedByGroup: byGroup,
                    selectedRows: rows,
                  };
                };

                const collectPriceSummaryLines = () => {
                  const lines = [];
                  const seen = new Set();
                  const pushLine = (raw) => {
                    const text = normalize(raw).replace(/\s+/g, ' ').trim();
                    if (!text) return;
                    if (text.length < 3 || text.length > 280) return;
                    const lowered = text.toLowerCase();
                    const hasPriceSignal = /€|eur|preis|price|netto|brutto|gross|inkl|mwst|vat|versand|shipping/.test(lowered);
                    if (!hasPriceSignal) return;
                    if (seen.has(text)) return;
                    seen.add(text);
                    lines.push(text);
                  };

                  const selectors = [
                    '[data-price]',
                    '[class*="price" i]',
                    '[id*="price" i]',
                    '[class*="summary" i]',
                    '[class*="total" i]',
                    '[class*="cart" i]',
                    '[class*="checkout" i]',
                    '[data-testid*="price" i]',
                  ];

                  const scoped = document.querySelectorAll(selectors.join(','));
                  scoped.forEach((el) => {
                    const raw = normalize(el.innerText || el.textContent || '');
                    if (!raw) return;
                    raw.split(/\n+/).forEach((line) => pushLine(line));
                  });

                  if (!lines.length && document.body) {
                    const bodyText = normalize(document.body.innerText || '');
                    bodyText.split(/\n+/).forEach((line) => pushLine(line));
                  }

                  return lines.slice(0, 120);
                };

                for (const [rawKey, rawValue] of Object.entries(requestedOptions)) {
                  const desired = normalizeLoose(rawValue);
                  if (!normalizeLoose(rawKey) || !desired) {
                    optionApplication.unmatched.push({ key: rawKey, value: rawValue, reason: 'empty_key_or_value' });
                    continue;
                  }

                  if (/(quantity|qty|auflage|menge|amount)/i.test(rawKey)) {
                    const qtyInput = quantityInputs.find((el) => !el.disabled);
                    if (qtyInput) {
                      qtyInput.focus();
                      qtyInput.value = String(rawValue);
                      qtyInput.dispatchEvent(new Event('input', { bubbles: true }));
                      qtyInput.dispatchEvent(new Event('change', { bubbles: true }));
                      await wait(300);
                      optionApplication.matched.push({ key: rawKey, value: rawValue, mode: 'quantity_input' });
                      optionApplication.strictPassMatched += 1;
                      continue;
                    }
                  }

                  let matchedResult = null;

                  const strictCandidates = scoreCandidates(rawKey, rawValue, false);
                  for (const candidate of strictCandidates) {
                    const applied = await applyControlValue(candidate.entry, rawValue);
                    if (!applied) continue;
                    matchedResult = {
                      key: rawKey,
                      value: rawValue,
                      group: candidate.entry.group,
                      controlTag: candidate.entry.tag,
                      controlLabel: candidate.entry.label,
                      score: candidate.score,
                      pass: 'strict',
                    };
                    optionApplication.strictPassMatched += 1;
                    break;
                  }

                  if (!matchedResult) {
                    const relaxedCandidates = scoreCandidates(rawKey, rawValue, true);
                    for (const candidate of relaxedCandidates) {
                      const applied = await applyControlValue(candidate.entry, rawValue);
                      if (!applied) continue;
                      matchedResult = {
                        key: rawKey,
                        value: rawValue,
                        group: candidate.entry.group,
                        controlTag: candidate.entry.tag,
                        controlLabel: candidate.entry.label,
                        score: candidate.score,
                        pass: 'relaxed',
                      };
                      optionApplication.relaxedPassMatched += 1;
                      break;
                    }
                  }

                  if (matchedResult) {
                    optionApplication.matched.push(matchedResult);
                  } else {
                    optionApplication.unmatched.push({
                      key: rawKey,
                      value: rawValue,
                      reason: 'no_control_match',
                    });
                  }
                }

                await wait(400);
                const postInteractionPriceCandidates = collectPriceCandidates();
                const chosenPriceCandidates = postInteractionPriceCandidates.length
                  ? postInteractionPriceCandidates
                  : preInteractionPriceCandidates;
                const selectedSnapshot = collectSelectedConfiguration();
                const priceSummaryLines = collectPriceSummaryLines();

                return {
                  optionGroups,
                  optionDependencies,
                  dependencyProbe: {
                    attemptedSteps: probeCandidates.length,
                    discoveredEdges: activeProbeDependencies.length,
                    activeProbeDependencies,
                  },
                  quantitySignal: {
                    mode,
                    hasManualInput: quantityInputs.length > 0,
                    presetValues: sortedPresets.slice(0, 50),
                    maxPreset,
                    hasThresholdBehavior: !!(quantityInputs.length && quantityPresets.size),
                  },
                  optionApplication,
                  domPriceCandidatesBefore: preInteractionPriceCandidates.slice(0, 25),
                  domPriceCandidatesAfter: postInteractionPriceCandidates.slice(0, 25),
                  domPriceCandidates: chosenPriceCandidates.slice(0, 25),
                  selectedConfiguration: selectedSnapshot.selectedByGroup,
                  selectedConfigurationRows: selectedSnapshot.selectedRows.slice(0, 120),
                  priceSummaryLines,
                };
              }
              """,
              {
                "dependencyProbeSteps": dependency_probe_steps,
                "requestedOptions": options_for_dom_apply,
                "optionValueAliases": option_value_aliases,
              },
          )
            result.option_groups = list(dom_signals.get("optionGroups") or [])
            result.option_dependencies = list(dom_signals.get("optionDependencies") or [])
            result.dependency_probe = dict(dom_signals.get("dependencyProbe") or {})
            option_application = dict(dom_signals.get("optionApplication") or {})
            option_application["requestedCount"] = len(requested_options_raw)
            option_application["matched"] = list(option_application.get("matched") or [])
            option_application["unmatched"] = list(option_application.get("unmatched") or [])
            option_application["strictPassMatched"] = int(option_application.get("strictPassMatched") or 0)
            option_application["relaxedPassMatched"] = int(option_application.get("relaxedPassMatched") or 0)

            if pre_applied_format_matches:
              option_application["matched"].extend(pre_applied_format_matches)
              option_application["strictPassMatched"] += len(pre_applied_format_matches)

            if pre_applied_format_unmatched:
              matched_keys = {str(row.get("key", "")).strip().lower() for row in pre_applied_format_matches}
              if matched_keys:
                option_application["unmatched"] = [
                  row
                  for row in option_application["unmatched"]
                  if str(row.get("key", "")).strip().lower() not in matched_keys
                ]

              if pre_applied_format_unmatched_keys:
                option_application["unmatched"] = [
                  row
                  for row in option_application["unmatched"]
                  if str(row.get("key", "")).strip().lower() not in pre_applied_format_unmatched_keys
                ]
              option_application["unmatched"].extend(pre_applied_format_unmatched)

            result.option_application = option_application
            result.quantity_signal = dict(dom_signals.get("quantitySignal") or {})
            result.dom_price_candidates_before = [
                float(v)
                for v in (dom_signals.get("domPriceCandidatesBefore") or [])
                if isinstance(v, (int, float))
            ]
            result.dom_price_candidates_after = [
                float(v)
                for v in (dom_signals.get("domPriceCandidatesAfter") or [])
                if isinstance(v, (int, float))
            ]
            result.dom_price_candidates = [
              float(v)
              for v in (dom_signals.get("domPriceCandidates") or [])
              if isinstance(v, (int, float))
            ]
            result.selected_configuration = dict(dom_signals.get("selectedConfiguration") or {})
            result.selected_configuration_rows = list(dom_signals.get("selectedConfigurationRows") or [])
            result.price_summary_lines = [
              str(v).strip()
              for v in (dom_signals.get("priceSummaryLines") or [])
              if str(v).strip()
            ]

            try:
                page.wait_for_timeout(500)
            except Exception:
              pass

            triggered = list(dict.fromkeys(request_urls[baseline_request_count:]))
            result.option_application["triggeredRequestUrls"] = triggered[:80]
            result.option_application["triggeredRequestCount"] = len(triggered)
        except Exception:
          # DOM introspection is best-effort and should not fail the run.
          pass

        if not result.option_application and requested_options_raw:
            result.option_application = {
              "requestedCount": len(requested_options_raw),
              "matched": list(pre_applied_format_matches),
              "unmatched": list(pre_applied_format_unmatched),
              "strictPassMatched": len(pre_applied_format_matches),
              "relaxedPassMatched": 0,
            }
        else:
          result.option_application = {
            "requestedCount": len(requested_options_raw),
            "matched": list(pre_applied_format_matches),
            "unmatched": list(pre_applied_format_unmatched),
            "strictPassMatched": len(pre_applied_format_matches),
            "relaxedPassMatched": 0,
            "callbackRedirectPlanned": dict(variant_callback_target or {}),
          }
          triggered = list(dict.fromkeys(request_urls[baseline_request_count:]))
          result.option_application["triggeredRequestUrls"] = triggered[:80]
          result.option_application["triggeredRequestCount"] = len(triggered)

      try:
        bootstrap_artifacts = page.evaluate(
          r"""
          async (ctx) => {
            const normalize = (v) => (v || '').toString().trim();
            const compact = (v) => normalize(v).replace(/\s+/g, ' ');
            const lower = (v) => compact(v).toLowerCase();
            const MAX_GROUPS = 40;
            const MAX_OPTIONS_PER_GROUP = 30;
            const MAX_HIDDEN_INPUTS = 60;
            const MAX_HINTS = 40;
            const MAX_SNIPPET = 320;

            const siteName = lower((ctx && ctx.siteName) || '');
            const dropdownMaps = [];

            const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

            const isVisible = (el) => {
              if (!el) return false;
              try {
                const style = window.getComputedStyle(el);
                if (style && (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0')) {
                  return false;
                }
              } catch (err) {
                void err;
              }
              try {
                const rect = el.getBoundingClientRect();
                return !!(rect && rect.width > 0 && rect.height > 0);
              } catch (err) {
                void err;
              }
              return false;
            };

            const pickFirst = (...values) => {
              for (const value of values) {
                const text = compact(value);
                if (text) return text;
              }
              return '';
            };

            const probeDropdowns = async () => {
              // Saxoprint (and similar SPAs) often render option nodes only after opening dropdowns.
              if (!siteName.includes('saxoprint')) return;

              const selectorCandidates = [
                'button[aria-haspopup="listbox"]',
                '[role="combobox"]',
                '[aria-controls][aria-expanded]',
                'button[aria-expanded]',
                'div[role="button"][aria-haspopup="listbox"]',
                '[data-testid*="select" i]',
                '[class*="select" i][role="button"]',
                '[class*="dropdown" i][role="button"]',
                '[class*="select" i] button',
              ];

              const seen = new Set();
              const triggers = [];
              selectorCandidates.forEach((sel) => {
                Array.from(document.querySelectorAll(sel)).slice(0, 80).forEach((el) => {
                  const key = el;
                  if (seen.has(key)) return;
                  seen.add(key);
                  if (!isVisible(el)) return;
                  triggers.push(el);
                });
              });

              const rankTrigger = (el) => {
                let score = 0;
                try {
                  const attrs = [
                    el.getAttribute && el.getAttribute('data-property-id'),
                    el.getAttribute && el.getAttribute('data-option-id'),
                    el.getAttribute && el.getAttribute('aria-haspopup'),
                    el.getAttribute && el.getAttribute('role'),
                    el.getAttribute && el.getAttribute('aria-controls'),
                  ].map((v) => lower(v));
                  if (attrs.some((v) => v && v.length >= 2)) score += 4;
                } catch (err) {
                  void err;
                }

                try {
                  const container = el.closest && el.closest('[data-property-id], [data-property], [data-option-id], [data-option-group], [class*="config" i], [class*="konfig" i], [class*="property" i], [class*="option" i]');
                  if (container) {
                    score += 12;
                    const cls = lower(container.getAttribute && container.getAttribute('class'));
                    const id = lower(container.getAttribute && container.getAttribute('id'));
                    if ((cls && (cls.includes('config') || cls.includes('konfig'))) || (id && (id.includes('config') || id.includes('konfig')))) {
                      score += 6;
                    }
                  }
                } catch (err) {
                  void err;
                }

                try {
                  const rect = el.getBoundingClientRect && el.getBoundingClientRect();
                  if (rect && rect.top >= 100 && rect.top <= 1400) score += 4;
                } catch (err) {
                  void err;
                }

                let label = '';
                try {
                  label = lower(pickFirst(
                    el.getAttribute && el.getAttribute('aria-label'),
                    el.getAttribute && el.getAttribute('title'),
                    el.innerText,
                    el.textContent,
                  ));
                } catch (err) {
                  void err;
                }
                if (label) {
                  if (label.includes('search') || label.includes('suche')) score -= 30;
                  if (label.includes('cookie') || label.includes('consent')) score -= 20;
                  if (label.includes('sprache') || label.includes('language')) score -= 12;
                }
                return score;
              };

              const rankedTriggers = triggers
                .map((el) => ({ el, score: rankTrigger(el) }))
                .sort((a, b) => b.score - a.score)
                .map((row) => row.el);

              const readTriggerText = (el, fallback) => {
                const direct = pickFirst(
                  el.getAttribute && el.getAttribute('aria-label'),
                  el.getAttribute && el.getAttribute('title'),
                  el.innerText,
                  el.textContent,
                );
                return direct || fallback || '';
              };

              const readGroupLabel = (el) => {
                const container = el.closest && el.closest(
                  'fieldset, [data-property], [data-option-group], [class*="option"], [class*="property"], [class*="config" i], [class*="konfig" i]'
                );
                if (!container) return '';
                const hint = pickFirst(
                  container.querySelector && container.querySelector('legend') && container.querySelector('legend').innerText,
                  container.querySelector && container.querySelector('label') && container.querySelector('label').innerText,
                  container.querySelector && container.querySelector('[class*="label" i]') && container.querySelector('[class*="label" i]').innerText,
                  container.querySelector && container.querySelector('[class*="title" i]') && container.querySelector('[class*="title" i]').innerText,
                  container.querySelector && container.querySelector('h1, h2, h3, h4, h5') && container.querySelector('h1, h2, h3, h4, h5').innerText,
                  container.getAttribute && container.getAttribute('aria-label'),
                );
                return hint || '';
              };

              const listCandidateScopes = () => {
                const selectors = [
                  '[role="listbox"]',
                  '[role="menu"]',
                  '[role="dialog"] [role="listbox"]',
                  '[role="dialog"] [role="menu"]',
                  '[aria-expanded="true"][role="listbox"]',
                  '[aria-expanded="true"][role="menu"]',
                  '[class*="listbox" i]',
                  '[class*="dropdown" i] ul',
                  '[class*="select" i] ul',
                  'ul[role]',
                  'ul',
                  'ol',
                ];

                const seen = new Set();
                const candidates = [];

                const add = (node) => {
                  if (!node || seen.has(node)) return;
                  seen.add(node);
                  if (!isVisible(node)) return;
                  const optCount = node.querySelectorAll('[role="option"]').length;
                  const liCount = node.querySelectorAll('li').length;
                  const btnCount = node.querySelectorAll('button, a, [role="menuitem"], [role="menuitemradio"], [role="menuitemcheckbox"]').length;
                  const count = Math.max(optCount, liCount, btnCount);
                  if (count < 2 || count > 120) return;
                  candidates.push({ node, count });
                };

                selectors.forEach((sel) => {
                  Array.from(document.querySelectorAll(sel)).slice(0, 220).forEach(add);
                });

                // Also include common portal roots near body end.
                Array.from(document.querySelectorAll('body > div, body > section')).slice(-40).forEach((node) => {
                  try {
                    if (!isVisible(node)) return;
                    if (node.querySelectorAll('[role="option"]').length >= 2) candidates.push({ node, count: node.querySelectorAll('[role="option"]').length });
                  } catch (err) {
                    void err;
                  }
                });

                return candidates;
              };

              const tryOpenTrigger = (triggerEl) => {
                if (!triggerEl) return;
                try {
                  triggerEl.focus && triggerEl.focus();
                } catch (err) {
                  void err;
                }
                // Some comboboxes only open on key events.
                const fireKey = (key) => {
                  try {
                    triggerEl.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));
                    triggerEl.dispatchEvent(new KeyboardEvent('keyup', { key, bubbles: true }));
                  } catch (err) {
                    void err;
                  }
                };
                fireKey('ArrowDown');
                fireKey('Enter');
                fireKey(' ');
              };

              const collectOpenOptionTexts = (trigger, preCandidates) => {
                const dedupe = (items) => {
                  const out = [];
                  items.forEach((t) => {
                    const text = compact(t);
                    if (!text) return;
                    if (!out.includes(text)) out.push(text);
                  });
                  return out;
                };

                const extractFromScope = (scope) => {
                  if (!scope) return [];
                  // Prefer ARIA options.
                  const roleOptions = Array.from(scope.querySelectorAll('[role="option"]')).filter(isVisible);
                  if (roleOptions.length >= 2) {
                    return dedupe(roleOptions.map((opt) => pickFirst(opt.innerText, opt.textContent, opt.getAttribute && opt.getAttribute('aria-label'), opt.getAttribute && opt.getAttribute('title')))).slice(0, 200);
                  }

                  // Fallback: list items and buttons.
                  const liItems = Array.from(scope.querySelectorAll('li')).filter(isVisible);
                  if (liItems.length >= 2) {
                    return dedupe(liItems.map((li) => pickFirst(li.innerText, li.textContent))).slice(0, 200);
                  }
                  const btnItems = Array.from(scope.querySelectorAll('button, a, [role="menuitem"], [role="menuitemradio"], [role="menuitemcheckbox"]')).filter(isVisible);
                  if (btnItems.length >= 2) {
                    return dedupe(btnItems.map((el) => pickFirst(el.innerText, el.textContent, el.getAttribute && el.getAttribute('aria-label'), el.getAttribute && el.getAttribute('title')))).slice(0, 200);
                  }
                  return [];
                };

                const pickScopeForTrigger = (triggerEl) => {
                  if (!triggerEl) return null;

                  const byId = (rawId) => {
                    const id = compact(rawId);
                    if (!id) return null;
                    try {
                      const node = document.getElementById(id);
                      if (node && isVisible(node)) return node;
                    } catch (err) {
                      void err;
                    }
                    return null;
                  };

                  // ARIA hook: aria-controls points directly at the popup/listbox.
                  const ctrl = byId(triggerEl.getAttribute && triggerEl.getAttribute('aria-controls'));
                  if (ctrl) return ctrl;

                  // aria-owns may contain one or more ids.
                  const ownsRaw = compact(triggerEl.getAttribute && triggerEl.getAttribute('aria-owns'));
                  if (ownsRaw) {
                    const ids = ownsRaw.split(/\s+/).filter(Boolean);
                    for (const oid of ids.slice(0, 3)) {
                      const owned = byId(oid);
                      if (owned) return owned;
                    }
                  }

                  let tRect = null;
                  try {
                    tRect = triggerEl.getBoundingClientRect();
                  } catch (err) {
                    void err;
                  }

                  const candidates = listCandidateScopes();

                  if (!candidates.length) return null;
                  if (!tRect) {
                    return candidates.sort((a, b) => b.count - a.count)[0].node;
                  }

                  const distanceScore = (node) => {
                    try {
                      const r = node.getBoundingClientRect();
                      const dy = Math.abs(r.top - tRect.bottom);
                      const dx = Math.abs(r.left - tRect.left);
                      // Prefer scopes close to trigger and below it.
                      const belowBonus = r.top >= tRect.bottom - 10 ? -40 : 0;
                      return dy * 1.3 + dx + belowBonus;
                    } catch (err) {
                      void err;
                      return 1e9;
                    }
                  };

                  // Prefer newly-visible candidates (not present before click), if provided.
                  if (Array.isArray(preCandidates) && preCandidates.length) {
                    const preNodes = preCandidates.map((c) => c.node);
                    const newlyVisible = candidates.filter((c) => !preNodes.includes(c.node));
                    if (newlyVisible.length) {
                      newlyVisible.sort((a, b) => distanceScore(a.node) - distanceScore(b.node));
                      return newlyVisible[0].node;
                    }
                  }

                  candidates.sort((a, b) => distanceScore(a.node) - distanceScore(b.node));
                  return candidates[0].node;
                };

                const scope = pickScopeForTrigger(trigger);
                const texts = extractFromScope(scope);
                return texts;
              };

              let idx = 0;
              for (const trigger of rankedTriggers.slice(0, 40)) {
                idx += 1;
                const triggerText = readTriggerText(trigger, `dropdown_${idx}`);
                const groupLabel = readGroupLabel(trigger);
                const labelLower = lower(groupLabel || triggerText);
                if (labelLower.includes('search') || labelLower.includes('suche')) {
                  continue;
                }
                if (
                  labelLower.includes('hauptmen') ||
                  labelLower.includes('produktmen') ||
                  labelLower.includes('kontaktmen') ||
                  labelLower.includes('menü umschalten') ||
                  labelLower.includes('menue umschalten') ||
                  labelLower.includes('benutzermen')
                ) {
                  continue;
                }
                // Snapshot scopes BEFORE click, to filter out always-visible menus.
                const preCandidates = listCandidateScopes();

                try {
                  trigger.click();
                } catch (err) {
                  void err;
                  continue;
                }
                tryOpenTrigger(trigger);

                await wait(140);
                const options = collectOpenOptionTexts(trigger, preCandidates);
                if (options.length >= 2) {
                  dropdownMaps.push({
                    triggerText,
                    triggerLabel: triggerText,
                    groupLabel: compact(groupLabel) || null,
                    optionCount: options.length,
                    options,
                  });
                }

                try {
                  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', bubbles: true }));
                } catch (err) {
                  void err;
                }
                try {
                  trigger.click();
                } catch (err) {
                  void err;
                }
                await wait(80);
              }
            };

            const cleanupGroupLabel = (value) => {
              let v = compact(value)
                .replace(/_essential$/i, '')
                .replace(/^input_var_/i, '')
                .replace(/^inputvar_/i, '')
                .replace(/[_-]+/g, ' ')
                .replace(/\s+/g, ' ')
                .trim();
              if (!v) return '';
              if (/^(?:[A-Z]{3,}\d{2,}|Z?[A-Z]{2,}\d{2,}[A-Z0-9_]*)$/.test(v)) {
                return '';
              }
              return v;
            };

            const safeAttrObject = (el) => {
              const attrs = {};
              if (!el || !el.attributes) return attrs;
              Array.from(el.attributes).forEach((attr) => {
                const name = lower(attr.name);
                if (!name) return;
                if (
                  name === 'id' ||
                  name === 'name' ||
                  name === 'type' ||
                  name === 'value' ||
                  name === 'href' ||
                  name === 'title' ||
                  name === 'role' ||
                  name === 'class' ||
                  name === 'action' ||
                  name === 'method' ||
                  name.startsWith('aria-') ||
                  name.startsWith('data-')
                ) {
                  attrs[name] = compact(attr.value);
                }
              });
              return attrs;
            };

            const collectRelevantDataAttributes = (el) => {
              const dataAttrs = {};
              if (!el || !el.attributes) return dataAttrs;
              Array.from(el.attributes).forEach((attr) => {
                const name = lower(attr.name);
                if (name && name.startsWith('data-') && compact(attr.value)) {
                  dataAttrs[name] = compact(attr.value);
                }
              });
              return dataAttrs;
            };

            const readBackendHints = (el) => {
              const dataAttributes = collectRelevantDataAttributes(el);
              return {
                dataVarindex: compact(el && el.getAttribute ? el.getAttribute('data-varindex') : ''),
                dataPrnumber: compact(el && el.getAttribute ? el.getAttribute('data-prnumber') : ''),
                dataPimvarnodekey: compact(el && el.getAttribute ? el.getAttribute('data-pimvarnodekey') : ''),
                dataOptionId: compact(el && el.getAttribute ? el.getAttribute('data-option-id') : ''),
                dataPropertyId: compact(el && el.getAttribute ? el.getAttribute('data-property-id') : ''),
                href: compact(el && el.getAttribute ? el.getAttribute('href') : ''),
                variantUrl: compact(el && el.getAttribute ? el.getAttribute('href') : ''),
                dataAttributes,
              };
            };

            const getAssociatedLabelText = (el) => {
              if (!el) return '';
              const id = compact(el.getAttribute('id'));
              if (id) {
                const linked = document.querySelector(`label[for="${id.replace(/"/g, '\\"')}"]`);
                if (linked && compact(linked.textContent)) return compact(linked.textContent);
              }

              const ariaLabel = compact(el.getAttribute('aria-label'));
              if (ariaLabel) return ariaLabel;

              const labelledBy = compact(el.getAttribute('aria-labelledby'));
              if (labelledBy) {
                const joined = labelledBy
                  .split(/\s+/)
                  .filter(Boolean)
                  .map((pid) => {
                    const node = document.getElementById(pid);
                    return compact(node ? node.textContent : '');
                  })
                  .filter(Boolean)
                  .join(' ')
                  .trim();
                if (joined) return joined;
              }

              const parentLabel = el.closest('label');
              if (parentLabel && compact(parentLabel.textContent)) {
                return compact(parentLabel.textContent);
              }

              return '';
            };

            const findGroupLabel = (el) => {
              const associated = cleanupGroupLabel(getAssociatedLabelText(el));
              if (associated) return associated;

              const attrLabel = compact(el.getAttribute('data-property-name') || el.getAttribute('data-label'));
              const cleanedAttrLabel = cleanupGroupLabel(attrLabel);
              if (cleanedAttrLabel) return cleanedAttrLabel;

              const fieldset = el.closest('fieldset');
              if (fieldset) {
                const legend = fieldset.querySelector('legend');
                const cleanedLegend = cleanupGroupLabel(legend ? legend.textContent : '');
                if (cleanedLegend) return cleanedLegend;
              }

              const section = el.closest('[data-property], [data-option-group], [class*="option"], [class*="property"]');
              if (section) {
                const heading = section.querySelector('h1, h2, h3, h4, h5, label');
                const cleanedHeading = cleanupGroupLabel(heading ? heading.textContent : '');
                if (cleanedHeading) return cleanedHeading;

                const semanticHint = cleanupGroupLabel(
                  section.getAttribute('data-property-name') ||
                  section.getAttribute('data-option-group') ||
                  section.getAttribute('data-name') ||
                  section.getAttribute('data-testid')
                );
                if (semanticHint) return semanticHint;
              }

              const fallback = cleanupGroupLabel(el.getAttribute('name') || el.getAttribute('id') || '');
              return fallback || 'unknown';
            };

            const getControlValue = (el) => {
              if (!el) return '';
              const tag = el.tagName.toLowerCase();
              if (tag === 'select') {
                const opt = el.options && el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
                return compact(
                  (opt && (opt.textContent || opt.getAttribute('label') || opt.value)) ||
                  el.getAttribute('value') ||
                  el.value ||
                  ''
                );
              }
              return pickFirst(
                el.getAttribute('data-label'),
                el.getAttribute('aria-label'),
                el.getAttribute('title'),
                el.getAttribute('value'),
                el.getAttribute('data-value'),
                el.textContent
              );
            };

            const getControlVisibleLabel = (el) => {
              if (!el) return '';
              const tag = el.tagName.toLowerCase();
              if (tag === 'select') {
                const opt = el.options && el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
                return pickFirst(
                  opt && opt.textContent,
                  opt && opt.getAttribute('label'),
                  opt && opt.value,
                  el.getAttribute('aria-label'),
                  el.getAttribute('title'),
                  el.getAttribute('name'),
                  el.getAttribute('id')
                );
              }
              return pickFirst(
                el.getAttribute('aria-label'),
                el.getAttribute('title'),
                el.getAttribute('data-label'),
                el.textContent,
                el.getAttribute('value'),
                el.getAttribute('name'),
                el.getAttribute('id')
              );
            };

            const isSelectedControl = (el) => {
              if (!el) return false;
              const tag = el.tagName.toLowerCase();
              if (tag === 'select') return true;
              if (tag === 'input') {
                const type = lower(el.getAttribute('type'));
                if ((type === 'radio' || type === 'checkbox') && el.checked) return true;
              }
              if (el.getAttribute('aria-selected') === 'true') return true;
              if (el.getAttribute('aria-pressed') === 'true') return true;
              if (tag === 'option' && el.selected) return true;
              return false;
            };

            const buildOptionRecord = (el, groupLabel, groupNormalized, groupElement, selectedOverride) => {
              const tag = lower(el.tagName);
              const type = tag === 'select'
                ? 'select'
                : lower(el.getAttribute('type')) || lower(el.getAttribute('role')) || tag;
              const backendHints = readBackendHints(el);
              return {
                groupLabel,
                normalizedGroupLabel: groupNormalized,
                visibleLabel: getControlVisibleLabel(el),
                visibleValue: getControlValue(el),
                selected: typeof selectedOverride === 'boolean' ? selectedOverride : isSelectedControl(el),
                controlTag: tag,
                controlType: type,
                name: compact(el.getAttribute('name')),
                id: compact(el.getAttribute('id')),
                href: compact(el.getAttribute('href')),
                variantUrl: compact(el.getAttribute('href')),
                backendHints,
                controlAttributes: safeAttrObject(el),
                groupAttributes: groupElement ? safeAttrObject(groupElement) : {},
                dataAttributes: backendHints.dataAttributes,
                ariaLabel: compact(el.getAttribute('aria-label')),
                title: compact(el.getAttribute('title')),
              };
            };

            const groupMap = new Map();
            const addToGroup = (groupLabel, groupElement, optionRecord) => {
              const normalized = lower(groupLabel || 'unknown');
              if (!groupMap.has(normalized)) {
                groupMap.set(normalized, {
                  groupLabel: groupLabel || 'unknown',
                  normalizedGroupLabel: normalized,
                  visibleGroupLabel: groupLabel || 'unknown',
                  controlTags: new Set(),
                  controlTypes: new Set(),
                  options: [],
                  selectedOptionCount: 0,
                  truncated: false,
                  groupAttributes: groupElement ? safeAttrObject(groupElement) : {},
                  sourceHints: groupElement ? {
                    dataPropertyName: compact(groupElement.getAttribute('data-property-name')),
                    dataOptionGroup: compact(groupElement.getAttribute('data-option-group')),
                    dataName: compact(groupElement.getAttribute('data-name')),
                    dataTestId: compact(groupElement.getAttribute('data-testid')),
                    id: compact(groupElement.getAttribute('id')),
                    className: compact(groupElement.getAttribute('class')),
                  } : {},
                  backendHints: {
                    dataVarindex: [],
                    dataPrnumber: [],
                    dataPimvarnodekey: [],
                    hrefs: [],
                    variantUrls: [],
                  },
                });
              }

              const group = groupMap.get(normalized);
              group.controlTags.add(optionRecord.controlTag);
              group.controlTypes.add(optionRecord.controlType);
              if (optionRecord.selected) group.selectedOptionCount += 1;

              const backendHints = optionRecord.backendHints || {};
              const pushUnique = (target, value) => {
                const text = compact(value);
                if (!text) return;
                if (!target.includes(text)) target.push(text);
              };
              pushUnique(group.backendHints.dataVarindex, backendHints.dataVarindex);
              pushUnique(group.backendHints.dataPrnumber, backendHints.dataPrnumber);
              pushUnique(group.backendHints.dataPimvarnodekey, backendHints.dataPimvarnodekey);
              pushUnique(group.backendHints.hrefs, backendHints.href);
              pushUnique(group.backendHints.variantUrls, backendHints.variantUrl);

              if (group.options.length < MAX_OPTIONS_PER_GROUP) {
                group.options.push(optionRecord);
              } else {
                group.truncated = true;
              }
            };

            const controls = Array.from(document.querySelectorAll(
              'input[type="radio"], input[type="checkbox"], select, button[aria-pressed], [role="option"], [data-option-id], [data-property-id], a[data-option-id], a[data-property-id], a[href*="depvar_index_setparent"], a[href*="otpmoreformats"], a[class*="format" i]'
            ));

            controls.slice(0, MAX_GROUPS * 100).forEach((el) => {
              const tag = lower(el.tagName);
              const groupLabel = findGroupLabel(el);
              const groupElement = el.closest('fieldset, [data-property], [data-option-group], [class*="option"], [class*="property"]') || el.parentElement;

              if (tag === 'select') {
                const options = Array.from(el.querySelectorAll('option'));
                const groupNormalized = lower(groupLabel || 'unknown');
                options.forEach((opt, idx) => {
                  const optionRecord = {
                    groupLabel,
                    normalizedGroupLabel: groupNormalized,
                    visibleLabel: pickFirst(opt.textContent, opt.getAttribute('label'), opt.value),
                    visibleValue: compact(opt.value || opt.getAttribute('value') || opt.textContent),
                    selected: !!opt.selected || idx === el.selectedIndex,
                    controlTag: 'option',
                    controlType: 'option',
                    name: compact(el.getAttribute('name')),
                    id: compact(opt.getAttribute('id')),
                    href: '',
                    variantUrl: '',
                    backendHints: {
                      dataVarindex: compact(el.getAttribute('data-varindex') || opt.getAttribute('data-varindex')),
                      dataPrnumber: compact(el.getAttribute('data-prnumber') || opt.getAttribute('data-prnumber')),
                      dataPimvarnodekey: compact(el.getAttribute('data-pimvarnodekey') || opt.getAttribute('data-pimvarnodekey')),
                      dataOptionId: compact(opt.getAttribute('data-option-id')),
                      dataPropertyId: compact(opt.getAttribute('data-property-id')),
                      href: '',
                      variantUrl: '',
                      dataAttributes: { ...collectRelevantDataAttributes(el), ...collectRelevantDataAttributes(opt) },
                    },
                    controlAttributes: { ...safeAttrObject(el), ...safeAttrObject(opt) },
                    groupAttributes: groupElement ? safeAttrObject(groupElement) : {},
                    dataAttributes: { ...collectRelevantDataAttributes(el), ...collectRelevantDataAttributes(opt) },
                    ariaLabel: compact(opt.getAttribute('aria-label')),
                    title: compact(opt.getAttribute('title')),
                  };
                  addToGroup(groupLabel, groupElement, optionRecord);
                });
                return;
              }

              const optionRecord = buildOptionRecord(el, groupLabel, lower(groupLabel || 'unknown'), groupElement);
              addToGroup(groupLabel, groupElement, optionRecord);
            });

            const optionCatalog = Array.from(groupMap.values()).slice(0, MAX_GROUPS).map((group) => ({
              groupLabel: group.groupLabel,
              normalizedGroupLabel: group.normalizedGroupLabel,
              visibleGroupLabel: group.visibleGroupLabel,
              controlTags: Array.from(group.controlTags),
              controlTypes: Array.from(group.controlTypes),
              optionCount: group.options.length + (group.truncated ? 1 : 0),
              capturedOptionCount: group.options.length,
              selectedOptionCount: group.selectedOptionCount,
              truncated: group.truncated,
              sourceHints: group.sourceHints,
              groupAttributes: group.groupAttributes,
              backendHints: group.backendHints,
              selectedOptions: group.options.filter((opt) => opt.selected).map((opt) => ({
                label: opt.visibleLabel,
                value: opt.visibleValue,
                controlTag: opt.controlTag,
                controlType: opt.controlType,
              })),
              options: group.options,
            }));

            const hiddenInputs = Array.from(document.querySelectorAll('form input[type="hidden"]')).slice(0, MAX_HIDDEN_INPUTS).map((el) => ({
              formId: compact(el.form && el.form.getAttribute ? el.form.getAttribute('id') : ''),
              formName: compact(el.form && el.form.getAttribute ? el.form.getAttribute('name') : ''),
              formAction: compact(el.form && el.form.getAttribute ? el.form.getAttribute('action') : ''),
              formMethod: lower(el.form && el.form.getAttribute ? el.form.getAttribute('method') : ''),
              name: compact(el.getAttribute('name')),
              value: compact(el.getAttribute('value')),
              id: compact(el.getAttribute('id')),
              type: compact(el.getAttribute('type')),
              dataAttributes: collectRelevantDataAttributes(el),
              backendHints: readBackendHints(el),
            }));

            const formSummaries = Array.from(document.querySelectorAll('form')).slice(0, 20).map((form) => {
              const hidden = Array.from(form.querySelectorAll('input[type="hidden"]')).slice(0, 20).map((el) => ({
                name: compact(el.getAttribute('name')),
                value: compact(el.getAttribute('value')),
                id: compact(el.getAttribute('id')),
                dataAttributes: collectRelevantDataAttributes(el),
                backendHints: readBackendHints(el),
              }));
              return {
                id: compact(form.getAttribute('id')),
                name: compact(form.getAttribute('name')),
                action: compact(form.getAttribute('action')),
                method: lower(form.getAttribute('method')) || 'get',
                hiddenInputCount: form.querySelectorAll('input[type="hidden"]').length,
                hiddenInputs: hidden,
                dataAttributes: collectRelevantDataAttributes(form),
                backendHints: readBackendHints(form),
              };
            });

            const setLinkCandidates = [];
            const addSetLinkCandidate = (value, source) => {
              const text = compact(value);
              if (!text) return;
              if (setLinkCandidates.some((entry) => entry.value === text && entry.source === source)) return;
              setLinkCandidates.push({ value: text, source });
            };

            ['SetLink', 'setLink', 'setlink'].forEach((key) => {
              try {
                addSetLinkCandidate(window[key], `window.${key}`);
              } catch (err) {
                void err;
              }
            });

            Array.from(document.querySelectorAll('[data-setlink], a[href*="setlink" i], form[action*="setlink" i], [href*="setlink" i]')).forEach((el) => {
              addSetLinkCandidate(el.getAttribute('data-setlink'), 'data-setlink');
              addSetLinkCandidate(el.getAttribute('href'), 'href');
              addSetLinkCandidate(el.getAttribute('action'), 'action');
              addSetLinkCandidate(el.textContent, 'text');
            });

            const scriptHints = [];
            Array.from(document.scripts).slice(0, 40).forEach((script, idx) => {
              const raw = compact(script.textContent);
              if (!raw) return;
              const lowered = raw.toLowerCase();
              const tokens = ['setlink', 'depvar_index_set', 'depvar', 'pimvarnodekey', 'prnumber', 'varindex'];
              const token = tokens.find((item) => lowered.includes(item));
              if (!token) return;
              const pos = lowered.indexOf(token);
              scriptHints.push({
                index: idx,
                src: compact(script.getAttribute('src')),
                token,
                snippet: raw.slice(Math.max(0, pos - 140), Math.min(raw.length, pos + MAX_SNIPPET)),
              });
            });

            const depvarHints = [];
            const hintSelector = 'a, button, input, select, option, form, script, [data-option-id], [data-property-id]';
            Array.from(document.querySelectorAll(hintSelector)).slice(0, 400).forEach((el) => {
              const attrs = safeAttrObject(el);
              Object.entries(attrs).forEach(([key, value]) => {
                const lowered = lower(value);
                if (!lowered) return;
                if (
                  lowered.includes('depvar') ||
                  lowered.includes('setlink') ||
                  lowered.includes('pimvarnodekey') ||
                  lowered.includes('prnumber') ||
                  lowered.includes('varindex') ||
                  lowered.includes('template')
                ) {
                  depvarHints.push({
                    source: 'dom',
                    key,
                    value,
                  });
                }
              });
            });

            const windowStateHints = [];
            const windowKeys = ['SetLink', 'setLink', 'depvar', 'depvarIndex', 'depvar_index_set', 'template', '__INITIAL_STATE__'];
            const previewWindowValue = (value) => {
              if (value === null || value === undefined) return '';
              if (typeof value === 'string') return compact(value);
              if (typeof value === 'number' || typeof value === 'boolean') return String(value);
              if (Array.isArray(value)) {
                const preview = value
                  .slice(0, 12)
                  .map((item) => {
                    if (item === null || item === undefined) return '';
                    if (typeof item === 'string' || typeof item === 'number' || typeof item === 'boolean') return String(item);
                    return '';
                  })
                  .filter(Boolean)
                  .join(', ');
                return preview ? `[${preview}]` : '';
              }
              try {
                const text = JSON.stringify(value);
                return text && text.length <= 1200 ? text : '';
              } catch (err) {
                void err;
                return '';
              }
            };

            windowKeys.forEach((key) => {
              try {
                const value = previewWindowValue(window[key]);
                if (value) {
                  windowStateHints.push({
                    source: 'window',
                    key,
                    value,
                  });
                }
              } catch (err) {
                void err;
              }
            });

            const currentSetLink = setLinkCandidates.length
              ? setLinkCandidates[0]
              : null;

            try {
              await probeDropdowns();
            } catch (err) {
              void err;
            }

            return {
              optionCatalog,
              requestTemplates: {
                currentSetLink,
                hiddenInputs,
                forms: formSummaries,
                depvarHints,
                scriptHints,
                pageStateHints: windowStateHints,
                setLinkCandidates: setLinkCandidates.slice(0, MAX_HINTS),
                dropdownMaps: dropdownMaps.slice(0, 30),
              },
            };
          }
          """
          ,
          {"siteName": result.site_name},
        )
      except Exception:
        bootstrap_artifacts = {}

      result.option_catalog, result.request_templates = extract_option_catalog_and_templates(bootstrap_artifacts)

      if True:
        def _compact_text(value: str | None) -> str:
          return " ".join(str(value or "").split()).strip()

        def _probe_dropdown_maps_playwright() -> list[dict[str, Any]]:
          if "saxoprint" not in (result.site_name or "").lower():
            return []

          dropdown_maps: list[dict[str, Any]] = []
          debug: dict[str, Any] = {
            "triggerCount": 0,
            "attempted": 0,
            "captured": 0,
            "samples": [],
          }
          irrelevant_two_item_sets = {
            ("produkte", "bestpreis-garantie"),
          }

          def _read_group_label_for(handle: Any) -> str:
            try:
              element = handle.element_handle()
            except Exception:
              element = None
            if element is None:
              return ""
            try:
              return _compact_text(
                page.evaluate(
                  r"""
                  (el) => {
                  const pickFirst = (...items) => {
                    for (const item of items) {
                    if (item === null || item === undefined) continue;
                    const text = String(item).replace(/\s+/g, ' ').trim();
                    if (text) return text;
                    }
                    return '';
                  };
                  const container = el.closest && el.closest(
                    'fieldset, [data-property], [data-option-group], [class*="option"], [class*="property"], [class*="config" i], [class*="konfig" i]'
                  );
                  if (!container) return '';
                  return pickFirst(
                    container.querySelector && container.querySelector('legend') && container.querySelector('legend').innerText,
                    container.querySelector && container.querySelector('label') && container.querySelector('label').innerText,
                    container.querySelector && container.querySelector('[class*="label" i]') && container.querySelector('[class*="label" i]').innerText,
                    container.querySelector && container.querySelector('[class*="title" i]') && container.querySelector('[class*="title" i]').innerText,
                    container.getAttribute && container.getAttribute('aria-label'),
                  );
                  }
                  """,
                  element,
                )
              )
            except Exception:
              return ""

          def _extract_options_from_scope(scope: Any) -> list[str]:
            try:
              option_locator = scope.locator('[role="option"]')
              count = option_locator.count()
              if 2 <= count <= 220:
                try:
                  texts = [_compact_text(t) for t in (option_locator.all_text_contents() or [])]
                except Exception:
                  texts = []
                return [t for t in texts if t][:200]

              # Lightning / SLDS comboboxes sometimes use custom elements/classes.
              custom_locator = scope.locator(
                'lightning-base-combobox-item, .slds-listbox__item, .slds-listbox__option, [data-value], [data-item], [data-option]'
              )
              count = custom_locator.count()
              if 2 <= count <= 260:
                try:
                  texts = [_compact_text(t) for t in (custom_locator.all_text_contents() or [])]
                except Exception:
                  texts = []
                return [t for t in texts if t][:200]

              li_locator = scope.locator('li')
              count = li_locator.count()
              if 2 <= count <= 260:
                try:
                  texts = [_compact_text(t) for t in (li_locator.all_text_contents() or [])]
                except Exception:
                  texts = []
                return [t for t in texts if t][:200]

              btn_locator = scope.locator('button, a, [role="menuitem"], [role="menuitemradio"], [role="menuitemcheckbox"]')
              count = btn_locator.count()
              if 2 <= count <= 260:
                try:
                  texts = [_compact_text(t) for t in (btn_locator.all_text_contents() or [])]
                except Exception:
                  texts = []
                return [t for t in texts if t][:200]
            except Exception:
              return []

            return []

          try:
            trigger_locator = page.locator(
              'input[placeholder*="Search for option" i], input[role="combobox"], [role="combobox"], button[aria-haspopup="listbox"], [aria-haspopup="listbox"]'
            )
            trigger_count = min(int(trigger_locator.count()), 18)
            debug["triggerCount"] = trigger_count
          except Exception:
            trigger_locator = None
            trigger_count = 0
            debug["error"] = "trigger_locator_failed"

          for i in range(trigger_count):
            trigger = trigger_locator.nth(i)

            try:
              if not trigger.is_visible(timeout=250):
                continue
            except Exception:
              continue

            try:
              aria_label = trigger.get_attribute("aria-label", timeout=250)
              title = trigger.get_attribute("title", timeout=250)
              placeholder = trigger.get_attribute("placeholder", timeout=250)
              value = None
              try:
                value = trigger.input_value(timeout=250)
              except Exception:
                value = None
              trigger_text = _compact_text(aria_label or title or placeholder or value)
            except Exception:
              trigger_text = ""
            group_label = _read_group_label_for(trigger)
            label_lower = (group_label or trigger_text).lower().strip()

            if not label_lower:
              continue
            if any(token in label_lower for token in ("search", "suche", "hauptmen", "produktmen", "kontaktmen", "benutzermen")):
              continue

            # Only attempt likely configurator controls.
            if group_label:
              gl = group_label.lower()
              if not any(token in gl for token in ("endformat", "material", "bindung", "seiten", "farb", "auflage", "ausf")):
                continue

            try:
              role_before = _compact_text(trigger.get_attribute("role", timeout=250))
              placeholder_before = _compact_text(trigger.get_attribute("placeholder", timeout=250))
              ctrl_before = _compact_text(trigger.get_attribute("aria-controls", timeout=250))
              owns_before = _compact_text(trigger.get_attribute("aria-owns", timeout=250))
            except Exception:
              role_before = ""
              placeholder_before = ""
              ctrl_before = ""
              owns_before = ""

            # If we have no way to locate the popup, skip (avoid grabbing unrelated always-visible menus).
            if not ctrl_before and not owns_before:
              continue

            # For Saxoprint configurator (vue-select), we expect listbox popups (often ids like vs*__listbox).
            popup_hint = f"{ctrl_before} {owns_before}".lower().strip()
            if "listbox" not in popup_hint:
              continue

            debug["attempted"] = int(debug.get("attempted") or 0) + 1

            try:
              pre_visible_ids = set(
                page.evaluate(
                  r"""
                  () => {
                  const isVisible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (!style) return false;
                    if (style.visibility === 'hidden' || style.display === 'none') return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 1 && r.height > 1;
                  };
                  return Array.from(document.querySelectorAll('[role="listbox"], [role="menu"]'))
                    .filter(isVisible)
                    .map((el) => String(el.id || ''))
                    .filter(Boolean);
                  }
                  """
                )
                or []
              )
            except Exception:
              pre_visible_ids = set()

            try:
              clicked = False

              # vue-select: clicking the input alone often only focuses.
              try:
                vs_toggle = trigger.locator('xpath=ancestor-or-self::*[contains(@class,"vs__dropdown-toggle")][1]')
              except Exception:
                vs_toggle = None

              if vs_toggle is not None:
                try:
                  if vs_toggle.count() > 0 and vs_toggle.is_visible(timeout=250):
                    vs_toggle.click(timeout=1200, no_wait_after=True)
                    clicked = True
                except Exception:
                  clicked = False

              if not clicked:
                trigger.click(timeout=1200, no_wait_after=True)
            except Exception:
              continue

            try:
              try:
                trigger.focus(timeout=800)
              except Exception:
                pass
              # Try common combobox open sequences.
              try:
                trigger.press("Alt+ArrowDown", timeout=600)
              except Exception:
                pass
              try:
                trigger.press("ArrowDown", timeout=600)
              except Exception:
                pass
              try:
                trigger.press("Enter", timeout=600)
              except Exception:
                pass
              page.keyboard.press("ArrowDown")
              page.keyboard.press("Enter")
            except Exception:
              pass

            # Wait briefly for vue-select listbox to populate after open.
            if owns_before:
              for oid in (owns_before.split() or [])[:2]:
                if not oid:
                  continue
                try:
                  page.wait_for_function(
                    r"""
                    (id) => {
                    const el = document.getElementById(id);
                    if (!el) return false;
                    const opts = el.querySelectorAll('[role="option"], .vs__dropdown-option, li');
                    return opts && opts.length >= 2;
                    }
                    """,
                    oid,
                    timeout=900,
                  )
                  break
                except Exception:
                  continue

            try:
              page.wait_for_timeout(180)
            except Exception:
              pass

            scope = None
            try:
              ctrl = _compact_text(trigger.get_attribute("aria-controls")) or ctrl_before
            except Exception:
              ctrl = ctrl_before

            if ctrl:
              scope = page.locator(f'[id="{ctrl}"]')
              try:
                scope.wait_for(state="visible", timeout=350)
              except Exception:
                pass
            else:
              try:
                owns = _compact_text(trigger.get_attribute("aria-owns")) or owns_before
              except Exception:
                owns = owns_before
              if owns:
                for oid in owns.split():
                  if not oid:
                    continue
                  candidate = page.locator(f'[id="{oid}"]')
                  try:
                    if candidate.count() > 0:
                      scope = candidate
                      break
                  except Exception:
                    continue

            if scope is None or (hasattr(scope, "count") and scope.count() == 0):
              try:
                post_visible_ids = set(
                  page.evaluate(
                    r"""
                    () => {
                    const isVisible = (el) => {
                      if (!el) return false;
                      const style = window.getComputedStyle(el);
                      if (!style) return false;
                      if (style.visibility === 'hidden' || style.display === 'none') return false;
                      const r = el.getBoundingClientRect();
                      return r.width > 1 && r.height > 1;
                    };
                    return Array.from(document.querySelectorAll('[role="listbox"], [role="menu"]'))
                      .filter(isVisible)
                      .map((el) => String(el.id || ''))
                      .filter(Boolean);
                    }
                    """
                  )
                  or []
                )
              except Exception:
                post_visible_ids = set()

              new_ids = [i for i in post_visible_ids if i not in pre_visible_ids]
              if new_ids:
                scope = page.locator(f'[id="{new_ids[0]}"]')
              else:
                scope = None

            if scope is None:
              if len(debug["samples"]) < 8:
                debug["samples"].append(
                  {
                    "groupLabel": group_label,
                    "triggerText": trigger_text,
                    "role": role_before,
                    "placeholder": placeholder_before,
                    "ariaControls": ctrl_before,
                    "ariaOwns": owns_before,
                    "result": "no_scope",
                  }
                )
              continue

            options = _extract_options_from_scope(scope)
            lowered = tuple([o.lower() for o in options[:2]]) if len(options) == 2 else tuple()
            if options and lowered in irrelevant_two_item_sets:
              options = []

            if len(options) >= 2:
              # Dedupe while preserving order.
              deduped: list[str] = []
              for opt in options:
                if opt and opt not in deduped:
                  deduped.append(opt)

              dropdown_maps.append(
                {
                  "triggerText": trigger_text or f"dropdown_{i+1}",
                  "triggerLabel": trigger_text or f"dropdown_{i+1}",
                  "groupLabel": group_label or None,
                  "optionCount": len(deduped),
                  "options": deduped[:200],
                }
              )
              debug["captured"] = int(debug.get("captured") or 0) + 1
            elif len(debug["samples"]) < 8:
              debug["samples"].append(
                {
                  "groupLabel": group_label,
                  "triggerText": trigger_text,
                  "role": role_before,
                  "placeholder": placeholder_before,
                  "ariaControls": ctrl_before,
                  "ariaOwns": owns_before,
                  "result": "no_options",
                }
              )

            try:
              page.keyboard.press("Escape")
            except Exception:
              pass

            if len(dropdown_maps) >= 18:
              break

          try:
            set_request_template_debug(result.request_templates, key="dropdownProbeDebug", payload=debug)
          except Exception:
            pass

          return dropdown_maps

        def _find_saxoprint_product_group_id_from_nuxt_payload() -> int | None:
          if "saxoprint" not in (result.site_name or "").lower():
            return None

          try:
            value = page.evaluate(
              r"""
              () => {
              try {
                if (typeof window.useNuxtApp !== 'function') return null;
                const app = window.useNuxtApp();
                const root = app && app.payload;
                if (!root || typeof root !== 'object') return null;

                // Common observed path on Saxoprint.
                try {
                  const direct = root.pinia && root.pinia.pct && root.pinia.pct.productGroupId;
                  if (typeof direct === 'number' && direct > 0) return direct;
                } catch (e) {
                  /* ignore */
                }

                // Generic bounded search for a numeric productGroupId.
                const seen = new Set();
                const queue = [{ obj: root, depth: 0 }];
                let visits = 0;
                while (queue.length && visits < 2000) {
                  const { obj, depth } = queue.shift();
                  visits++;
                  if (!obj || typeof obj !== 'object') continue;
                  if (seen.has(obj)) continue;
                  seen.add(obj);
                  if (depth > 7) continue;
                  for (const [k, v] of Object.entries(obj)) {
                    if (String(k).toLowerCase() === 'productgroupid') {
                      const n = Number(v);
                      if (Number.isFinite(n) && n > 0) return n;
                    }
                    if (v && typeof v === 'object') {
                      queue.push({ obj: v, depth: depth + 1 });
                    }
                  }
                }
                return null;
              } catch (e) {
                return null;
              }
              }
              """
            )
          except Exception:
            return None

          try:
            pgid = int(value or 0)
          except Exception:
            pgid = 0
          return pgid if pgid > 0 else None

        def _build_saxoprint_dropdown_maps_via_nuxt_t() -> list[dict[str, Any]]:
          if "saxoprint" not in (result.site_name or "").lower():
            return []

          product_group_id = _find_saxoprint_product_group_id_from_nuxt_payload() or find_saxoprint_product_group_id_from_traces(network_traces)
          if not product_group_id:
            try:
              set_request_template_debug(
                result.request_templates,
                key="saxoprintTDebug",
                payload={
                  "ok": False,
                  "error": "no_product_group_id",
                },
              )
            except Exception:
              pass
            return []

          trace_body = find_saxoprint_get_product_values_body_from_traces(network_traces)
          portalcode = find_saxoprint_portalcode_from_traces(network_traces)

          try:
            response = page.evaluate(
              r"""
              async ({ productGroupId, traceBody, portalcode }) => {
              try {
                if (typeof window.useNuxtApp !== 'function') {
                  return { ok: false, error: 'no_useNuxtApp' };
                }
                const app = window.useNuxtApp();
                const t = app && (app.$t || app.t);
                if (typeof t !== 'function') {
                  return { ok: false, error: 'no_t' };
                }

                // Sanity check that t() works for numeric resourceIds.
                const sanity = String(t('326') || '').trim();
                if (!sanity) {
                  return { ok: false, error: 't_sanity_failed' };
                }

                const body = (traceBody && typeof traceBody === 'object') ? traceBody : {
                  productGroupId: Number(productGroupId),
                  propertyConfiguration: [],
                  propertyConfigurationCode: null,
                };
                body.productGroupId = Number(body.productGroupId || productGroupId);

                const resp = await fetch('https://api.saxoprint.de/product-configuration/get-product-values', {
                  method: 'POST',
                  credentials: 'include',
                  headers: {
                    'content-type': 'application/json',
                    'accept': 'application/json, text/plain, */*',
                    ...(portalcode ? { 'portalcode': String(portalcode) } : {}),
                  },
                  body: JSON.stringify(body),
                });
                if (!resp.ok) {
                  let text = '';
                  try {
                    text = String(await resp.text());
                  } catch (e) {
                    text = '';
                  }
                  return {
                    ok: false,
                    error: 'fetch_failed',
                    status: resp.status,
                    responsePreview: text ? text.slice(0, 400) : null,
                    usedBody: {
                      productGroupId: body.productGroupId,
                      propertyConfigurationCount: Array.isArray(body.propertyConfiguration) ? body.propertyConfiguration.length : null,
                      hasPropertyConfigurationCode: body.propertyConfigurationCode !== undefined,
                      portalcode: portalcode ? String(portalcode) : null,
                    },
                  };
                }
                const payload = await resp.json();
                const options = Array.isArray(payload && payload.propertyOptions) ? payload.propertyOptions : [];

                const dropdownMaps = [];
                for (const opt of options) {
                  if (!opt || typeof opt !== 'object') continue;
                  const propertyId = Number(opt.id || 0);
                  if (!propertyId) continue;
                  if (opt.isRange) continue;

                  const tooltipRaw = (opt.tooltip !== undefined && opt.tooltip !== null) ? String(opt.tooltip) : '';
                  const tooltipText = tooltipRaw
                    .replace(/<[^>]+>/g, ' ')
                    .replace(/\s+/g, ' ')
                    .trim();
                  const tooltipLower = tooltipText.toLowerCase();
                  let contextHint = null;
                  if (tooltipLower.includes('umschlag')) {
                    contextHint = 'cover';
                  } else if (tooltipLower.includes('broschüreninhalt') || tooltipLower.includes('inhalt')) {
                    contextHint = 'content';
                  }

                  const propertyResourceId = opt.resourceId;
                  const groupLabel = String(t(String(propertyResourceId)) || '').replace(/\s+/g, ' ').trim();
                  const values = Array.isArray(opt.propertyValueOptions) ? opt.propertyValueOptions : [];

                  const labels = [];
                  const valueIds = [];
                  for (const v of values) {
                    if (!v || typeof v !== 'object') continue;
                    const valueId = Number(v.id || 0);
                    if (!valueId) continue;
                    const rid = v.resourceId;
                    if (!rid) continue;
                    const label = String(t(String(rid)) || '').replace(/\s+/g, ' ').trim();
                    if (!label) continue;
                    labels.push(label);
                    valueIds.push(valueId);
                  }

                  if (labels.length >= 1 && labels.length === valueIds.length) {
                    // Keep artifact size bounded.
                    if (labels.length > 80) continue;
                    dropdownMaps.push({
                      source: 'saxoprint_get_product_values_nuxt_t',
                      propertyId,
                      groupLabel: groupLabel || null,
                      triggerText: groupLabel || `property_${propertyId}`,
                      triggerLabel: groupLabel || `property_${propertyId}`,
                      contextHint,
                      tooltipPreview: tooltipText ? tooltipText.slice(0, 200) : null,
                      optionCount: labels.length,
                      options: labels.slice(0, 200),
                      valueIds: valueIds.slice(0, 200),
                    });
                  }
                }

                return {
                  ok: true,
                  productGroupId: Number(body.productGroupId),
                  sanity,
                  dropdownMaps: dropdownMaps.slice(0, 60),
                };
              } catch (e) {
                return { ok: false, error: String(e && e.message ? e.message : e) };
              }
              }
              """,
              {"productGroupId": int(product_group_id), "traceBody": trace_body, "portalcode": portalcode},
            )
          except Exception:
            return []

          if not isinstance(response, dict):
            return []
          if not bool(response.get("ok")):
            try:
              set_request_template_debug(result.request_templates, key="saxoprintTDebug", payload=dict(response))
            except Exception:
              pass
            return []

          dropdown_maps = response.get("dropdownMaps")
          if not isinstance(dropdown_maps, list):
            dropdown_maps = []
          return [row for row in dropdown_maps if isinstance(row, dict)]

        try:
          dropdown_maps_pw = _probe_dropdown_maps_playwright()
          if dropdown_maps_pw:
            set_dropdown_maps(result.request_templates, dropdown_maps_pw, limit=30)
        except Exception:
          pass

        # Preferred for Saxoprint: build label maps from get-product-values + Nuxt translation ($t).
        try:
          dropdown_maps_t = _build_saxoprint_dropdown_maps_via_nuxt_t()
          if dropdown_maps_t:
            set_dropdown_maps(result.request_templates, dropdown_maps_t, limit=60)
        except Exception:
          pass

      if "csrf" in content:
          token_indicators.add("csrf")
      if "captcha" in content or "are you human" in content:
          saw_waf_status = True

      try:
          cookies = context.cookies()
          if cookies:
              result.requires_session = True
              result.cookie_names = sorted({str(cookie.get("name", "")) for cookie in cookies if cookie.get("name")})
              result.cookies = {
                  str(cookie.get("name")): str(cookie.get("value"))
                  for cookie in cookies
                  if cookie.get("name") and cookie.get("value")
              }
      except Exception:
          pass

      if (not headless) and debug_hold_seconds > 0:
          try:
              page.wait_for_timeout(int(max(0.0, float(debug_hold_seconds)) * 1000))
          except Exception:
              pass

      browser.close()

    result.option_url_variants = variant_url_signals

    if variant_callback_target is not None:
      target_url = str(variant_callback_target.get("targetUrl") or "").strip()
      option_key = str(variant_callback_target.get("key") or "")
      option_value = variant_callback_target.get("value")
      if not target_url:
        result.warnings.append("variant_callback_target_missing_url")
      elif target_url == product_url:
        result.warnings.append("variant_callback_target_same_as_current_url")
      elif target_url in visited_urls:
        result.warnings.append(f"variant_callback_target_already_visited: {target_url}")
      elif variant_callback_depth >= 2:
        result.warnings.append("variant_callback_max_depth_reached")
      else:
        next_requested = dict(requested_options_raw)
        next_requested.pop(option_key, None)
        redirected = bootstrap_product_url(
          product_url=target_url,
          headless=headless,
          timeout_ms=timeout_ms,
          max_observed_requests=max_observed_requests,
          dependency_probe_steps=dependency_probe_steps,
          requested_options=next_requested,
          auto_accept_cookies=auto_accept_cookies,
          debug_hold_seconds=debug_hold_seconds,
          variant_callback_depth=variant_callback_depth + 1,
          visited_variant_urls=visited_urls.union({product_url}),
        )

        redirected.option_url_variants = list(variant_url_signals) + list(redirected.option_url_variants)
        merged_application = dict(redirected.option_application or {})
        merged_application["requestedCount"] = len(requested_options_raw)
        merged_application["matched"] = list(merged_application.get("matched") or [])
        merged_application["unmatched"] = list(merged_application.get("unmatched") or [])
        merged_application["strictPassMatched"] = int(merged_application.get("strictPassMatched") or 0)

        callback_row = {
          "key": option_key,
          "value": option_value,
          "group": "product_url_variant",
          "controlTag": "url",
          "controlLabel": str(variant_callback_target.get("label") or ""),
          "score": int(variant_callback_target.get("score") or 0),
          "pass": "variant_url_callback",
          "variantUrl": target_url,
        }
        merged_application["matched"] = [callback_row] + merged_application["matched"]
        merged_application["strictPassMatched"] += 1
        merged_application["unmatched"] = [
          row
          for row in merged_application["unmatched"]
          if str(row.get("key", "")).strip().lower() != option_key.strip().lower()
        ]
        merged_application["callbackRedirect"] = {
          "fromUrl": product_url,
          "toUrl": target_url,
          "depth": variant_callback_depth + 1,
        }
        redirected.option_application = merged_application
        redirected.warnings.insert(0, f"variant_callback_redirect option={option_key} to={target_url}")
        return redirected

    result.observed_requests = request_urls
    result.network_traces = network_traces
    result.token_indicators = sorted(token_indicators)
    result.anti_bot_suspected = saw_waf_status
    return result
